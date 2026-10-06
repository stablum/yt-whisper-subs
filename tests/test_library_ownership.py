"""Protect unfinished work from pruning, live-owner recovery, and other writers.

Example: `python -m unittest tests.test_library_ownership` uses isolated catalogs.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import test_library as fixtures
from yt_whisper_subs import library_app
from yt_whisper_subs import chapters
from yt_whisper_subs import library_db
from yt_whisper_subs import library_job_db
from yt_whisper_subs import library_service
from yt_whisper_subs import library_types as types
from yt_whisper_subs import output_lock
from yt_whisper_subs import pipeline
from yt_whisper_subs import pipeline_progress as progress


class LibraryOwnershipTests(unittest.TestCase):
    """Exercise actual SQLite state and OS locks without touching the real app.

    Example: a child process cannot take the parent's pipeline lock.
    """

    def setUp(self) -> None:
        """Initialize a new catalog containing an ordinary remote video.

        Example: the video has no media, watch state, or download history.
        """

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db = library_db.LibraryDb(self.root / "library" / "catalog.sqlite3")
        self.db.initialize()
        self.video_id = "aaaaaaaaaaa"
        self.channel = self.db.add_channel("https://www.youtube.com/@example/videos", "Example", True)
        self.db.upsert_video(fixtures.make_meta(self.video_id, "Working", 100), self.channel.channel_id)

    def test_all_pruning_paths_preserve_running_paused_and_interrupted_jobs(self) -> None:
        """Keep unfinished work protected while untouched remote rows remain prunable.

        Example: cutoff, bounded snapshot, and unsubscription use the same guard.
        """

        for state in types.PipelineJobState:
            for mode in ("cutoff", "snapshot", "unsubscribe"):
                with self.subTest(state=state, mode=mode), tempfile.TemporaryDirectory() as tmp:
                    db = library_db.LibraryDb(Path(tmp) / "catalog.sqlite3")
                    db.initialize()
                    channel = db.add_channel("https://www.youtube.com/@example/videos", "Example", True)
                    db.upsert_video(fixtures.make_meta(self.video_id, "Working", 100), channel.channel_id)
                    db.upsert_video(fixtures.make_meta("bbbbbbbbbbb", "Untouched", 100), channel.channel_id)
                    db.begin_pipeline_job(self.video_id, types.PipelineKind.DOWNLOAD)
                    with db._connect() as conn:
                        conn.execute("UPDATE pipeline_jobs SET state=?", (state.value,))
                    if mode == "cutoff":
                        self.assertEqual(db.prune_remote_before(200), 1)
                    elif mode == "snapshot":
                        snapshot = types.ChannelSnapshot(channel.url, "UC-example", "Example", [], True)
                        self.assertEqual(db.store_snapshot(channel.channel_id, snapshot).pruned, 1)
                    else:
                        db.remove_channel(channel.channel_id)
                    self.assertIsNotNone(db.video(self.video_id))
                    self.assertEqual(db.pipeline_job(self.video_id).state, state)
                    self.assertIsNone(db.video("bbbbbbbbbbb"))

    def test_second_catalog_client_does_not_recover_live_or_paused_owner(self) -> None:
        """Recognize a running process even when another client opens its database.

        Example: paused work belongs to the same live owner as running work.
        """

        self.db.begin_pipeline_job(self.video_id, types.PipelineKind.DOWNLOAD)
        second = library_db.LibraryDb(self.db.path)
        for paused in (False, True):
            self.db.set_pipeline_job_paused(self.video_id, paused)
            self.assertEqual(second.recover_pipeline_jobs(), [])
            expected = types.PipelineJobState.PAUSED if paused else types.PipelineJobState.RUNNING
            self.assertEqual(self.db.pipeline_job(self.video_id).state, expected)

    def test_dead_owner_recovers_with_progress_and_retention_protection(self) -> None:
        """Restore only genuinely abandoned execution while retaining its checkpoint.

        Example: a dead child's PID remains recoverable after publication cutoff.
        """

        child = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True, check=True)
        self.db.begin_pipeline_job(self.video_id, types.PipelineKind.DOWNLOAD)
        update = progress.make(self.video_id, progress.Stage.TRANSCRIBING, 0.5)
        self.db.update_pipeline_job(update)
        with self.db._connect() as conn:
            conn.execute("UPDATE pipeline_jobs SET owner_pid=?", (int(child.stdout),))
        self.assertEqual(self.db.prune_remote_before(200), 0)
        jobs = self.db.recover_pipeline_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].state, types.PipelineJobState.INTERRUPTED)
        self.assertEqual(jobs[0].stage, progress.Stage.TRANSCRIBING.value)
        self.assertAlmostEqual(jobs[0].fraction, progress.overall_fraction(update))

    def test_reused_pid_does_not_claim_an_old_job(self) -> None:
        """Include process creation time in live-owner identity.

        Example: an unrelated newer process with the same PID cannot block recovery.
        """

        self.db.begin_pipeline_job(self.video_id, types.PipelineKind.DOWNLOAD)
        with self.db._connect() as conn:
            conn.execute("UPDATE pipeline_jobs SET owner_started_at=owner_started_at-1")
        self.assertEqual(self.db.recover_pipeline_jobs()[0].state, types.PipelineJobState.INTERRUPTED)

    def test_unknown_process_access_does_not_claim_it_is_dead(self) -> None:
        """Preserve a job when ownership cannot be disproved.

        Example: permission denial is not treated as a crashed process.
        """

        self.db.begin_pipeline_job(self.video_id, types.PipelineKind.DOWNLOAD)
        with mock.patch.object(library_job_db.psutil, "Process", side_effect=library_job_db.psutil.AccessDenied(os.getpid())):
            self.assertEqual(self.db.recover_pipeline_jobs(), [])

    def test_existing_checkpoint_schema_gains_owner_columns_without_losing_jobs(self) -> None:
        """Evolve the existing catalog in place without erasing recovery state.

        Example: pre-change checkpoints remain present and acquire nullable owners.
        """

        self.db.begin_pipeline_job(self.video_id, types.PipelineKind.DOWNLOAD)
        with self.db._connect() as conn:
            conn.execute("ALTER TABLE pipeline_jobs DROP COLUMN owner_pid")
            conn.execute("ALTER TABLE pipeline_jobs DROP COLUMN owner_started_at")
        self.db.initialize()
        jobs = self.db.recover_pipeline_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].video_id, self.video_id)
        with self.db._connect() as conn:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_os_lock_excludes_second_process_and_releases_after_exit(self) -> None:
        """Test actual process exclusion rather than mocking a lock implementation.

        Example: a child gets Busy while the parent owns a pipeline root.
        """

        code = (
            "import sys; from pathlib import Path; from yt_whisper_subs import output_lock; "
            "lock=output_lock.OutputLock(Path(sys.argv[1]), 'pipeline'); "
            "\ntry:\n lock.__enter__()\nexcept output_lock.OutputBusyError:\n print('BUSY')\n"
        )
        with output_lock.OutputLock(self.root, "pipeline"):
            child = subprocess.run([sys.executable, "-c", code, str(self.root)], capture_output=True, text=True, check=True)
            self.assertEqual(child.stdout.strip(), "BUSY")
        with output_lock.OutputLock(self.root, "pipeline"):
            pass

    def test_killed_owner_releases_os_lock_without_stale_file_cleanup(self) -> None:
        """Let OS ownership disappear after abrupt process termination.

        Example: a leftover `.lock` file does not prevent a new run.
        """

        code = (
            "import sys,time; from pathlib import Path; from yt_whisper_subs import output_lock; "
            "lock=output_lock.OutputLock(Path(sys.argv[1]), 'pipeline'); "
            "lock.__enter__(); print('LOCKED',flush=True); time.sleep(30)"
        )
        child = subprocess.Popen([sys.executable, "-c", code, str(self.root)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "LOCKED")
            with self.assertRaises(output_lock.OutputBusyError):
                with output_lock.OutputLock(self.root, "pipeline"):
                    pass
        finally:
            child.kill()
            child.communicate(timeout=5)
        with output_lock.OutputLock(self.root, "pipeline"):
            pass

    def test_library_launch_is_rejected_before_catalog_access(self) -> None:
        """Block a second GUI before initialization, scanning, or job recovery.

        Example: the first instance's catalog is never opened by the duplicate.
        """

        with output_lock.OutputLock(self.root, "library"), mock.patch.object(library_app.proc, "configure_stdio"), mock.patch.object(library_app.QtWidgets.QApplication, "instance", return_value=mock.Mock()), mock.patch.object(library_app.QtWidgets.QMessageBox, "information") as message, mock.patch.object(library_app.library_service, "LibraryService") as service:
            self.assertEqual(library_app.main(["--out-dir", str(self.root)]), 0)
        service.assert_not_called()
        message.assert_called_once()

    def test_service_does_not_recover_while_an_orphan_child_still_writes(self) -> None:
        """Respect pipeline ownership even after its catalog owner has died.

        Example: an orphaned CLI child can finish before recovery is offered.
        """

        service = library_service.LibraryService(self.root, {"python": Path(sys.executable)},
            feed=fixtures.FakeFeed([]), downloader=fixtures.FakeDownloader(self.root))
        service.db.begin_pipeline_job(self.video_id, types.PipelineKind.DOWNLOAD)
        with service.db._connect() as conn:
            conn.execute("UPDATE pipeline_jobs SET owner_started_at=0")
        with output_lock.OutputLock(self.root, "pipeline"):
            self.assertEqual(service.recover_pipeline_jobs(), [])
        self.assertEqual(service.recover_pipeline_jobs()[0].state, types.PipelineJobState.INTERRUPTED)

    def test_runner_does_not_touch_yields_when_pipeline_root_is_busy(self) -> None:
        """Keep the shared CLI path from modifying another writer's files.

        Example: a blocked runner never starts preparation or downloading.
        """

        runner = pipeline.PipelineRunner(mock.Mock(), {}, self.root, self.root / "log")
        with output_lock.OutputLock(self.root, "pipeline"), mock.patch.object(runner, "_run_locked") as run:
            with self.assertRaises(output_lock.OutputBusyError):
                runner.run()
        run.assert_not_called()

    def test_removal_does_not_touch_another_pipeline_writer(self) -> None:
        """Apply shared write ownership to deletion as well as generation.

        Example: a CLI extraction prevents GUI removal of its source video.
        """

        service = library_service.LibraryService(self.root, {"python": Path(sys.executable)},
            feed=fixtures.FakeFeed([]), downloader=fixtures.FakeDownloader(self.root))
        service.video_dir.mkdir()
        video = service.video_dir / f"{self.video_id}.mkv"
        video.write_bytes(b"video")
        service.scan_local()
        manifest = service.video_yields(self.video_id)
        with output_lock.OutputLock(self.root, "pipeline"):
            with self.assertRaises(output_lock.OutputBusyError):
                service.remove_yields(manifest)
        self.assertTrue(video.exists())
        self.assertFalse(service.db.video(self.video_id).removed)

    def test_independent_playback_does_not_rewrite_chapters_while_root_is_busy(self) -> None:
        """Retain playback during another pipeline without competing file writes.

        Example: missing derived FFmetadata is deferred while a writer owns the root.
        """

        service = library_service.LibraryService(self.root, {"python": Path(sys.executable)},
            feed=fixtures.FakeFeed([]), downloader=fixtures.FakeDownloader(self.root))
        files = chapters.ChapterFiles.for_video(self.root, self.video_id)
        files.write(chapters.ChapterSet(self.video_id, "nl", "mock", 1, 90_000,
            [chapters.Chapter(0, "Onderwerp", "Topic")]))
        with output_lock.OutputLock(self.root, "pipeline"):
            self.assertEqual(service._playback_chapters(self.video_id), files.mpv)
            files.mpv.unlink()
            self.assertIsNone(service._playback_chapters(self.video_id))
            self.assertFalse(files.mpv.exists())
        self.assertEqual(service._playback_chapters(self.video_id), files.mpv)
