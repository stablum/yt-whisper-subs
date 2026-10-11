"""Verify filtered cleanup scope and durable history against isolated libraries.

Example: `python -m unittest tests.test_library_removal` never deletes user downloads.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore
from PySide6 import QtGui
from PySide6 import QtWidgets

from tests import test_library as fixtures
from yt_whisper_subs import library_db
from yt_whisper_subs import library_model
from yt_whisper_subs import library_removal_actions
from yt_whisper_subs import library_service
from yt_whisper_subs import library_types as types
from yt_whisper_subs import library_views as views
from yt_whisper_subs import library_widgets
from yt_whisper_subs import library_yields
from yt_whisper_subs import output_lock
from yt_whisper_subs import playback_progress
from yt_whisper_subs import task_cancel


class RemovalFixture:
    """Share isolated library setup; e.g. service tests and Qt scope tests use the same records."""

    def setUp(self) -> None:
        """Seed four tiny downloads with known watch states; e.g. exactly 95% remains partial."""

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.downloader = fixtures.FakeDownloader(self.root)
        self.service = library_service.LibraryService(
            self.root, {"python": Path("python")},
            feed=fixtures.FakeFeed([]), downloader=self.downloader,
        )
        self.ids = ("aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc", "ddddddddddd")
        titles = ("Wanted watched", "Other watched", "Wanted partial", "Wanted second channel")
        for video_id, title in zip(self.ids, titles, strict=True):
            self.service.db.upsert_video(fixtures.make_meta(video_id, title))
            self.downloader.download(self.service.db.video(video_id), lambda _: None)
        self.service.scan_local()
        for video_id, position in zip(self.ids, (96, 100, 95, 97), strict=True):
            self.service.db.record_playback(playback_progress.make(video_id, position, 100))


class FilteredRemovalTests(RemovalFixture, unittest.TestCase):
    """Exercise actual file ownership and SQLite history; e.g. watched cleanup and restart."""

    def test_watched_filter_removes_all_yields_and_retains_history_after_restart(self) -> None:
        """Honor existing watched classification and exact ownership; e.g. keep the 95% video."""

        before = self.service.db.video(self.ids[0])
        for folder, suffix in (
            ("audio", ".opus"), ("metadata", ".info.json"), ("subtitles", ".en.srt"),
            ("chapters", ".chapters.json"), ("logs", "-today.log"),
        ):
            directory = self.root / folder
            directory.mkdir(exist_ok=True)
            (directory / f"{self.ids[0]}{suffix}").write_bytes(b"yield")
        unrelated = self.root / "subtitles" / f"{self.ids[0]}2.srt"
        unrelated.write_bytes(b"keep")
        model = library_model.VideoTableModel()
        model.set_records(self.service.db.videos(), {video_id: None for video_id in self.ids})
        proxy = library_model.VideoFilterModel()
        proxy.setSourceModel(model)
        proxy.set_view(views.VideoView.WATCHED)
        matched = [proxy.index(row, 0).data(QtCore.Qt.ItemDataRole.UserRole).meta.identity.video_id
                   for row in range(proxy.rowCount())]
        self.assertEqual(set(matched), {self.ids[0], self.ids[1], self.ids[3]})
        plan = self.service.yield_selection(matched, lambda _: None)
        self.assertEqual(plan.file_count, 8)
        self.assertEqual(plan.size_bytes, 40)
        self.assertTrue(before.local.path.exists(), "preview must be read-only")

        result = self.service.remove_yield_selection(plan, lambda _: None)

        self.assertEqual(result, library_yields.RemovalResult(3, 8))
        self.assertTrue(unrelated.exists())
        reopened = library_db.LibraryDb(self.service.db.path)
        reopened.initialize()
        removed = reopened.video(self.ids[0])
        self.assertTrue(removed.removed)
        self.assertEqual(removed.playback, before.playback)
        self.assertEqual(removed.history.downloaded_at, before.history.downloaded_at)
        self.assertTrue(reopened.video(self.ids[2]).downloaded)
        self.assertIsNone(reopened.video(self.ids[2]).playback.completed_at)
        empty = self.service.yield_selection(matched, lambda _: None)
        self.assertEqual(empty.items, ())

    def test_one_pass_inventory_for_many_ids(self) -> None:
        """Keep directory scans independent of match count; e.g. 500 watched videos."""

        folders = ("videos", "audio", "metadata", "subtitles", "chapters", "logs")
        for folder in folders:
            (self.root / folder).mkdir(exist_ok=True)
        original = Path.iterdir
        scans: list[Path] = []

        def scan(path: Path):
            """Observe filesystem traversal; e.g. videos is scanned once for the whole batch."""

            scans.append(path)
            return original(path)

        with mock.patch.object(Path, "iterdir", scan):
            manifests = library_yields.VideoYields.inspect_many(self.root, self.ids)
        self.assertEqual(len(manifests), 4)
        self.assertEqual(scans, [self.root / folder for folder in folders])

    def test_pending_and_playing_videos_are_skipped_at_preview_and_execution(self) -> None:
        """Recheck protected work after confirmation; e.g. mpv opened during the preview."""

        self.service.db.begin_pipeline_job(self.ids[0], types.PipelineKind.DOWNLOAD)
        player = mock.Mock()
        self.service._playback.activate(self.ids[1], player)
        plan = self.service.yield_selection(self.ids, lambda _: None, excluded=frozenset({self.ids[2]}))
        self.assertEqual([item.yields.video_id for item in plan.items], [self.ids[3]])
        self.assertEqual(len(plan.skipped), 3)
        self.service._playback.activate(self.ids[3], mock.Mock())
        result = self.service.remove_yield_selection(plan, lambda _: None)
        self.assertEqual(result, library_yields.RemovalResult(0, 0, 4))
        self.assertTrue(all(self.service.db.video(video_id).downloaded for video_id in self.ids))

    def test_new_queue_and_job_after_preview_are_protected(self) -> None:
        """Preserve newly pending work; e.g. another repair was queued after inspection."""

        plan = self.service.yield_selection(self.ids, lambda _: None)
        self.service.db.begin_pipeline_job(self.ids[1], types.PipelineKind.DOWNLOAD)
        result = self.service.remove_yield_selection(
            plan, lambda _: None, excluded=frozenset({self.ids[0]}),
        )
        self.assertEqual(result, library_yields.RemovalResult(2, 2, 2))
        self.assertTrue(self.service.db.video(self.ids[0]).downloaded)
        self.assertIsNotNone(self.service.db.pipeline_job(self.ids[1]))

    def test_stale_manifest_aborts_whole_batch_before_removal(self) -> None:
        """Never remove newly created unapproved yields; e.g. another tool wrote a log."""

        plan = self.service.yield_selection(self.ids, lambda _: None)
        directory = self.root / "logs"
        directory.mkdir()
        extra = directory / f"{self.ids[-1]}-new.log"
        extra.write_bytes(b"new")
        with self.assertRaisesRegex(RuntimeError, "confirmed manifest"):
            self.service.remove_yield_selection(plan, lambda _: None)
        self.assertTrue(extra.exists())
        self.assertTrue(all(self.service.db.video(video_id).local.path.exists() for video_id in self.ids))

    def test_foreign_or_misattributed_manifest_cannot_delete_any_files(self) -> None:
        """Validate every manifest before the first unlink; e.g. a different video's file."""

        plan = self.service.yield_selection(self.ids[:2], lambda _: None)
        foreign = self.root / "protected.txt"
        foreign.write_bytes(b"keep")
        for path in (foreign, self.service.db.video(self.ids[2]).local.path):
            forged = library_yields.VideoYields(self.root, self.ids[1], (path,))
            bad_plan = plan._replace(items=(plan.items[0], library_yields.YieldItem("Forged", forged)))
            with self.assertRaisesRegex(RuntimeError, "refusing yield"):
                self.service.remove_yield_selection(bad_plan, lambda _: None)
            self.assertTrue(path.exists())
            self.assertTrue(self.service.db.video(self.ids[0]).local.path.exists())

    def test_cancel_keeps_completed_removal_and_leaves_remaining_downloads(self) -> None:
        """Reconcile completed videos when cancellation interrupts the batch; e.g. stop after one."""

        plan = self.service.yield_selection(self.ids, lambda _: None)

        def cancel(message: str) -> None:
            """Stop before the second video's files; e.g. a worker cancellation checkpoint."""

            if message == f"Removing video · {self.ids[1]}":
                raise task_cancel.CancelledError()

        with self.assertRaises(task_cancel.CancelledError):
            self.service.remove_yield_selection(plan, cancel)
        self.assertTrue(self.service.db.video(self.ids[0]).removed)
        self.assertTrue(all(self.service.db.video(video_id).downloaded for video_id in self.ids[1:]))

    def test_redirected_managed_folder_preserves_protected_files(self) -> None:
        """Reject directory links even when files match; e.g. audio is a Windows junction."""

        protected = self.root / "protected"
        protected.mkdir()
        file = protected / f"{self.ids[0]}.opus"
        file.write_bytes(b"keep")
        redirected = self.root / "audio"
        if os.name == "nt":
            try:
                subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(redirected), str(protected)],
                    creationflags=subprocess.CREATE_NO_WINDOW, capture_output=True, check=True,
                )
            except subprocess.CalledProcessError as exc:
                if b"Access is denied" in exc.stderr:
                    self.skipTest("environment forbids junction creation")
                raise
        else:
            redirected.symlink_to(protected, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "redirected managed folder"):
            self.service.yield_selection(self.ids, lambda _: None)
        self.assertTrue(file.exists())

    def test_file_error_keeps_incomplete_video_out_of_removed_state(self) -> None:
        """Reconcile partial file deletion without claiming success; e.g. a locked video file."""

        audio_dir = self.root / "audio"
        audio_dir.mkdir()
        audio = audio_dir / f"{self.ids[0]}.opus"
        audio.write_bytes(b"audio")
        plan = self.service.yield_selection(self.ids, lambda _: None)
        video = self.service.db.video(self.ids[0]).local.path
        unlink = Path.unlink

        def locked(path: Path, *args, **kwargs):
            """Keep just the first video locked; e.g. another player owns its file."""

            if path == video:
                raise PermissionError("locked")
            return unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", locked):
            with self.assertRaises(PermissionError):
                self.service.remove_yield_selection(plan, lambda _: None)
        self.assertFalse(audio.exists())
        self.assertTrue(video.exists())
        self.assertFalse(self.service.db.video(self.ids[0]).removed)
        self.assertTrue(all(self.service.db.video(video_id).downloaded for video_id in self.ids[1:]))

    def test_pipeline_owner_blocks_cleanup_before_any_unlink(self) -> None:
        """Respect the shared writer lock; e.g. a CLI pipeline is downloading a video."""

        plan = self.service.yield_selection(self.ids, lambda _: None)
        with output_lock.OutputLock(self.root, "pipeline"):
            with self.assertRaises(RuntimeError):
                self.service.remove_yield_selection(plan, lambda _: None)
        self.assertTrue(all(self.service.db.video(video_id).local.path.exists() for video_id in self.ids))


class FilteredRemovalUiTests(RemovalFixture, unittest.TestCase):
    """Use real Qt proxy filters while stubbing dispatch; e.g. Watched plus channel and search."""

    @classmethod
    def setUpClass(cls) -> None:
        """Own one offscreen Qt application; e.g. no user's library window is opened."""

        cls._app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_filter_snapshot_combines_scope_and_search_without_row_selection(self) -> None:
        """Capture the actual proxy intersection; e.g. only Wanted watched in channel one."""

        model = library_model.VideoTableModel()
        records = [self.service.db.video(video_id)._replace(subscription_id=1 if idx < 3 else 2)
                   for idx, video_id in enumerate(self.ids)]
        model.set_records(records, {video_id: None for video_id in self.ids})
        proxy = library_model.VideoFilterModel()
        proxy.setSourceModel(model)
        proxy.set_view(views.VideoView.WATCHED)
        proxy.set_channels(frozenset({1}))
        proxy.set_search("Wanted")
        window = SimpleNamespace(
            _busy=False, _ui=SimpleNamespace(catalog=SimpleNamespace(proxy=proxy)),
            _service=self.service, _video_queue={}, _run_task=mock.Mock(),
            _remove_inspected=mock.Mock(),
        )
        library_removal_actions.RemovalActionsMixin._remove_filtered(window)
        inspect = window._run_task.call_args.args[1]
        proxy.set_search("")
        proxy.set_view(views.VideoView.ALL)
        selection = inspect(lambda _: None)
        self.assertEqual([item.yields.video_id for item in selection.items], [self.ids[0]])
        self.assertTrue(self.service.db.video(self.ids[0]).local.path.exists())

    def test_confirmation_defaults_to_cancel_and_shows_exact_scope(self) -> None:
        """Make destructive scope reviewable; e.g. two matches list all paths and titles."""

        window = QtWidgets.QMainWindow()
        plan = self.service.yield_selection(self.ids[:2], lambda _: None)
        with mock.patch.object(QtWidgets.QMessageBox, "exec", return_value=QtWidgets.QMessageBox.StandardButton.Cancel):
            accepted = library_removal_actions.RemovalActionsMixin._confirm_yields(window, plan, filtered=True)
        self.assertFalse(accepted)
        box = window.findChild(QtWidgets.QMessageBox)
        self.assertEqual(box.defaultButton(), box.button(QtWidgets.QMessageBox.StandardButton.Cancel))
        self.assertIn("2 matching video(s)", box.text())
        self.assertIn("captured", box.informativeText())
        self.assertIn(plan.items[0].title, box.detailedText())
        self.assertIn(str(plan.items[1].yields.paths[0]), box.detailedText())

    def test_filter_menu_shares_action_availability(self) -> None:
        """Keep menu state in one QAction; e.g. a busy worker disables both entry points."""

        bar = library_widgets.SmartFilterBar()
        action = QtGui.QAction("Remove filtered downloads…", bar)
        bar.set_removal_action(action)
        button = bar.findChild(QtWidgets.QToolButton, "filterActions")
        self.assertIs(button.menu().actions()[0], action)
        action.setEnabled(False)
        self.assertFalse(button.menu().actions()[0].isEnabled())
