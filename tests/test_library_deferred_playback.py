"""Exercise double-click playback intent through real Qt signals and catalog state.

Example: `python -m unittest tests.test_library_deferred_playback`.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore  # noqa: E402
from PySide6 import QtWidgets  # noqa: E402

from tests import test_library as fixtures  # noqa: E402
from yt_whisper_subs import library_gui  # noqa: E402
from yt_whisper_subs import library_service  # noqa: E402
from yt_whisper_subs import library_types as types  # noqa: E402
from yt_whisper_subs import output_lock  # noqa: E402
from yt_whisper_subs import pipeline_progress as progress  # noqa: E402
from yt_whisper_subs import task_cancel  # noqa: E402


class DeferredPlaybackTests(unittest.TestCase):
    """Verify playback targets and outcomes without external downloads or mpv.

    Example: finishing a controlled worker exercises the production completion slot.
    """

    @classmethod
    def setUpClass(cls) -> None:
        """Keep one Qt application for the real table and worker signals.

        Example: every test activates a production proxy index.
        """

        cls._app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self) -> None:
        """Create two remote rows and capture worker dispatch for controlled outcomes.

        Example: captured workers can finish or cancel without launching a CLI child.
        """

        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
        self._service = library_service.LibraryService(self._root, {"python": Path(sys.executable)})
        for video_id, title in (("aaaaaaaaaaa", "First"), ("bbbbbbbbbbb", "Second")):
            ident = types.VideoIdentity(video_id, f"https://youtu.be/{video_id}", title)
            origin = types.VideoOrigin("Channel", "UC-example", 100)
            details = types.VideoDetails(90, None, "", None, "not_live")
            self._service.db.upsert_video(types.VideoMeta(ident, origin, details))
        self._tasks = []
        self._players = []
        self._open_window()

    def _open_window(self) -> None:
        """Capture workers before the Qt startup timer can dispatch real CLI work."""

        self._window = library_gui.LibraryWindow(self._service)
        self._window._media_timer.stop()
        self._window._timer.stop()
        self._window._metadata_timer.stop()
        patches = (
            mock.patch.object(self._window._pool, "start", side_effect=self._tasks.append),
            mock.patch.object(self._window._playback_pool, "start", side_effect=self._players.append),
            mock.patch.object(self._service, "play"),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self) -> None:
        """Dispose the test window and watchers before removing isolated files.

        Example: a nonmodal failure dialog is closed with its parent window.
        """

        self._close_window()
        self._tmp.cleanup()

    def _close_window(self) -> None:
        """Dispose of the current window so another can reopen the same catalog."""

        self._window._quitting = True
        self._window._timer.stop()
        self._window._metadata_timer.stop()
        self._window._media_timer.stop()
        self._window._pipeline_timer.stop()
        self._window._tray.hide()
        self._window.close()
        self._window.deleteLater()
        self._app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def _finish_task(self, video_id: str, task) -> None:
        """Simulate successful publication and the worker's corresponding Qt signal."""

        self._service.db.finish_pipeline_job(video_id)
        task.signals.finished.emit(None)

    def _activate(self, video_id: str) -> None:
        """Emit the table's actual double-click signal for a stable video ID.

        Example: sorting cannot redirect `_activate("aaaaaaaaaaa")` to the second row.
        """

        catalog = self._window._ui.catalog
        row = catalog.model.row_for(video_id)
        index = catalog.proxy.mapFromSource(catalog.model.index(row, 0))
        catalog.table.doubleClicked.emit(index)

    def _make_ready(self, video_id: str) -> None:
        """Reconcile usable media and subtitle yields into the real SQLite catalog.

        Example: completion reads this fresh record even while another row is selected.
        """

        video_dir = self._root / "videos"
        video_dir.mkdir(exist_ok=True)
        (video_dir / f"{video_id}.mkv").write_bytes(b"video")
        cue = "1\n00:00:00,000 --> 00:00:01,000\nHello.\n"
        for suffix in ("srt", "en.srt"):
            (video_dir / f"{video_id}.{suffix}").write_text(cue, encoding="utf-8")
        self._service.scan_local()

    def test_repeated_activation_plays_once_despite_selection_and_filter_changes(self) -> None:
        """Keep initial processing silent and pin one later playback request to its ID.

        Example: selecting and filtering to B still opens completed A exactly once.
        """

        self._activate("aaaaaaaaaaa")
        self.assertEqual(self._window._play_after_pipeline, set())
        self.assertEqual(len(self._tasks), 1)
        self._activate("aaaaaaaaaaa")
        self._activate("aaaaaaaaaaa")
        self.assertEqual(self._window._play_after_pipeline, {"aaaaaaaaaaa"})
        self.assertIn("Will play when processing finishes", self._window.statusBar().currentMessage())
        self.assertEqual(self._players, [])
        self._activate("bbbbbbbbbbb")
        self._window._ui.header.search.setText("Second")
        self._make_ready("aaaaaaaaaaa")
        self._finish_task("aaaaaaaaaaa", self._tasks[0])

        self.assertEqual(self._window._active_video_id, "bbbbbbbbbbb")
        self.assertEqual(self._window._play_after_pipeline, set())
        self.assertEqual(len(self._players), 1)
        self._players[0].run()
        self.assertEqual(self._service.play.call_args.args[0], "aaaaaaaaaaa")
        self._service.play.assert_called_once()

    def test_queued_request_survives_another_videos_completion(self) -> None:
        """Retain the waiting target's intent until that specific pipeline finishes.

        Example: finishing A advances to B without consuming B's playback request.
        """

        self._activate("aaaaaaaaaaa")
        self._activate("bbbbbbbbbbb")
        self._activate("bbbbbbbbbbb")
        self._make_ready("aaaaaaaaaaa")
        self._finish_task("aaaaaaaaaaa", self._tasks[0])
        self.assertEqual(self._window._play_after_pipeline, {"bbbbbbbbbbb"})
        self.assertEqual(self._players, [])
        self._make_ready("bbbbbbbbbbb")
        self._finish_task("bbbbbbbbbbb", self._tasks[1])

        self.assertEqual(len(self._players), 1)
        self._players[0].run()
        self.assertEqual(self._service.play.call_args.args[0], "bbbbbbbbbbb")

    def test_downloaded_paused_work_waits_for_worker_success(self) -> None:
        """Give pending work precedence over playable files and early Ready events.

        Example: chapter regeneration waits through pause and final catalog scanning.
        """

        self._make_ready("aaaaaaaaaaa")
        self._window.refresh()
        self._activate("aaaaaaaaaaa")
        self._players.clear()
        self._window._generate_chapters_selected()
        task = self._tasks[0]
        task.signals.progress.emit(progress.encode(progress.make("aaaaaaaaaaa", progress.Stage.PAUSED)))
        self._activate("aaaaaaaaaaa")
        task.signals.progress.emit(progress.encode(progress.make("aaaaaaaaaaa", progress.Stage.READY)))
        self._activate("aaaaaaaaaaa")
        self.assertEqual(self._players, [])
        self._finish_task("aaaaaaaaaaa", task)

        self.assertEqual(len(self._players), 1)
        self.assertEqual(self._window._play_after_pipeline, set())

    def test_failure_and_cancellation_clear_only_the_active_request(self) -> None:
        """Suppress playback after unsuccessful work while preserving waiting intent.

        Example: cancelling A leaves B queued with its own playback request.
        """

        for outcome in ("failed", "cancelled"):
            with self.subTest(outcome=outcome):
                self._activate("aaaaaaaaaaa")
                self._activate("aaaaaaaaaaa")
                self._activate("bbbbbbbbbbb")
                self._activate("bbbbbbbbbbb")
                task = self._tasks[-1]
                if outcome == "failed":
                    self._service.db.interrupt_pipeline_job("aaaaaaaaaaa", failed=True)
                    task.signals.failed.emit("Pipeline failed", "Controlled failure")
                else:
                    self._service.db.finish_pipeline_job("aaaaaaaaaaa")
                    task.signals.cancelled.emit()
                self.assertEqual(self._players, [])
                self.assertEqual(self._window._play_after_pipeline, {"bbbbbbbbbbb"})
                self._service.db.finish_pipeline_job("bbbbbbbbbbb")
                self._tasks[-1].signals.cancelled.emit()
                self.assertEqual(self._window._play_after_pipeline, set())
                self._service.db.cancel_waiting_pipeline("aaaaaaaaaaa")

    def test_incomplete_success_does_not_process_a_different_selected_row(self) -> None:
        """Check the requested catalog record before any deferred playback launch.

        Example: missing A yields must not start a fresh download of selected B.
        """

        self._activate("aaaaaaaaaaa")
        self._activate("aaaaaaaaaaa")
        self._window._ui.catalog.table.clearSelection()
        self._finish_task("aaaaaaaaaaa", self._tasks[0])

        self.assertEqual(self._players, [])
        self.assertEqual(len(self._tasks), 1)
        self.assertEqual(self._window._play_after_pipeline, set())

    def test_quitting_suppresses_deferred_playback(self) -> None:
        """Consume a completed request without launching a player during shutdown.

        Example: queued completion signals arriving after Quit remain harmless.
        """

        self._activate("aaaaaaaaaaa")
        self._activate("aaaaaaaaaaa")
        self._make_ready("aaaaaaaaaaa")
        self._window._quitting = True
        self._finish_task("aaaaaaaaaaa", self._tasks[0])

        self.assertEqual(self._players, [])
        self.assertEqual(self._window._play_after_pipeline, set())

    def test_quit_preserves_translation_and_waiting_jobs_for_automatic_restart(self) -> None:
        """Reopen the catalog and drain work in FIFO order through real worker code."""

        first, second = "aaaaaaaaaaa", "bbbbbbbbbbb"
        update = progress.make(first, progress.Stage.TRANSLATING, 0.0, "Preparing translation")
        self._activate(first)
        self._activate(second)
        self._activate(second)
        self.assertEqual(len(self._tasks), 1)
        model = self._window._ui.catalog.model
        self.assertEqual(model.progress_at(model.row_for(second)).label, "Queued · next")

        def close_during_translation(_record: types.VideoRecord, report) -> None:
            """Stop at the same checkpoint as the reported restart interruption."""

            report(progress.encode(update))
            self._window._prepare_to_quit()
            task_cancel.current().checkpoint()

        with mock.patch.object(self._service._downloader, "download", side_effect=close_during_translation):
            self._tasks[0].run()
        job = self._service.db.pipeline_job(first)
        self.assertEqual(job.state, types.PipelineJobState.INTERRUPTED)
        self.assertEqual(job.stage, progress.Stage.TRANSLATING)
        self.assertEqual(job.fraction, progress.overall_fraction(update))
        self.assertEqual(self._service.db.pipeline_job(second).state, types.PipelineJobState.QUEUED)

        self._close_window()
        self._tasks.clear()
        downloader = fixtures.FakeDownloader(self._root)
        self._service = library_service.LibraryService(self._root, {"python": Path(sys.executable)}, downloader=downloader)
        self._open_window()
        self._app.processEvents()
        self.assertEqual(self._window._active_video_id, first)
        self._tasks[0].run()
        self.assertEqual(self._window._active_video_id, second)
        self._tasks[1].run()
        self.assertEqual(downloader.calls, [first, second])
        self.assertEqual(self._service.db.pipeline_jobs(), [])
        self.assertEqual(self._players, [])

    def test_startup_rechecks_orphaned_pipeline_after_the_writer_releases_its_lock(self) -> None:
        """Keep a crashed job when another process may still be producing yields."""

        video_id = "aaaaaaaaaaa"
        fixtures.claim_download(self._service.db, video_id)
        with self._service.db._connect() as conn:
            conn.execute("UPDATE pipeline_jobs SET owner_started_at=0")
        with output_lock.OutputLock(self._root, "pipeline"):
            self._app.processEvents()
            self.assertEqual(self._tasks, [])
            self.assertTrue(self._window._pipeline_timer.isActive())
            self.assertEqual(self._service.db.pipeline_job(video_id).state, types.PipelineJobState.RUNNING)
        self._window._pipeline_timer.timeout.emit()
        self.assertEqual(self._window._active_video_id, video_id)
        self.assertEqual(len(self._tasks), 1)

    def test_explicit_cancel_discards_waiting_dispatch_before_quit(self) -> None:
        """Cancel an assigned worker even when its service call has yet to begin."""

        self._activate("aaaaaaaaaaa")
        self._activate("bbbbbbbbbbb")
        self._window._cancel_active_task()
        self._window._prepare_to_quit()
        self._tasks[0].run()
        self.assertEqual([job.video_id for job in self._service.db.pipeline_jobs()], ["bbbbbbbbbbb"])

    def test_due_retry_runs_after_later_ready_work(self) -> None:
        """Dispatch a deferred job from the timer without blocking the ready FIFO."""

        first, second = "aaaaaaaaaaa", "bbbbbbbbbbb"
        self._activate(first)
        self._activate(second)
        downloader = fixtures.FakeDownloader(self._root)
        with mock.patch.object(self._service._downloader, "download", side_effect=RuntimeError("offline")):
            self._tasks[0].run()
        retry_at = self._service.db.pipeline_job(first).retry_at
        self.assertEqual(self._window._active_video_id, second)
        with mock.patch.object(self._service._downloader, "download", side_effect=downloader.download):
            self._tasks[1].run()
            self.assertEqual(downloader.calls, [second])
            self.assertTrue(self._window._pipeline_timer.isActive())
            with mock.patch("yt_whisper_subs.library_video_queue.time.time", return_value=retry_at):
                self._window._pipeline_timer.timeout.emit()
                self.assertEqual(self._window._active_video_id, first)
                self._tasks[2].run()
        self.assertEqual(downloader.calls, [second, first])
        self.assertEqual(self._service.db.pipeline_jobs(), [])


if __name__ == "__main__":
    unittest.main()
