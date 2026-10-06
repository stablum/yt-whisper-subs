"""Protect personal video history throughout removal, retention, and rediscovery.

Example: `python -m unittest tests.test_library_history` uses temporary libraries.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore
from PySide6 import QtWidgets

from tests import test_library as fixtures
from yt_whisper_subs import library_db
from yt_whisper_subs import library_model
from yt_whisper_subs import library_service
from yt_whisper_subs import library_types as types
from yt_whisper_subs import library_views as views
from yt_whisper_subs import library_widgets
from yt_whisper_subs import library_yields
from yt_whisper_subs import playback_progress as playback
from yt_whisper_subs import pipeline_progress as progress


class VideoHistoryTests(unittest.TestCase):
    """Exercise real SQLite history with exact per-video file removal.

    Example: a pre-cutoff watched download remains after removal and restart.
    """

    def setUp(self) -> None:
        """Create an isolated library with two remote entries and fake clients.

        Example: `self.service` never accesses YouTube or the user's catalog.
        """

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.feed = fixtures.FakeFeed([
            fixtures.make_meta("aaaaaaaaaaa", "History", 100),
            fixtures.make_meta("bbbbbbbbbbb", "Untouched", 100),
        ])
        self.downloader = fixtures.FakeDownloader(self.root)
        self.service = library_service.LibraryService(
            self.root, {"python": Path("python")},
            feed=self.feed, downloader=self.downloader,
        )
        self.channel = self.service.track_channel("@example", True)
        self.service.initialize_channel(self.channel.channel_id)

    def _download(self) -> types.VideoRecord:
        """Materialize a download and reconcile its durable history.

        Example: `_download()` gives the removal tests a real media file.
        """

        record = self.service.db.video("aaaaaaaaaaa")
        self.downloader.download(record, lambda _: None)
        self.service.scan_local()
        return self.service.db.video("aaaaaaaaaaa")

    def test_full_removal_survives_cutoff_restart_and_channel_pruning(self) -> None:
        """Retain downloaded/watched history while still pruning untouched rows.

        Example: removal before 2026 stays visible even after unsubscription.
        """

        initial = self._download()
        for folder, suffix in (
            ("audio", ".opus"), ("metadata", ".info.json"),
            ("subtitles", ".en.srt"), ("chapters", ".chapters.json"),
            ("logs", "-20261007-120000.log"),
        ):
            directory = self.root / folder
            directory.mkdir(exist_ok=True)
            (directory / f"aaaaaaaaaaa{suffix}").write_text("yield", encoding="utf-8")
        self.service.db.record_playback(playback.make("aaaaaaaaaaa", 96, 100))
        watched_at = self.service.db.video("aaaaaaaaaaa").playback.completed_at
        self.service.db.set_auto_pending({"aaaaaaaaaaa"}, True)
        self.service.db.begin_pipeline_job("aaaaaaaaaaa", types.PipelineKind.DOWNLOAD)
        self.service.db.set_setting("channel_published_after", "2026-01-01")

        removed = self.service.remove_yields(self.service.video_yields("aaaaaaaaaaa"))

        self.assertEqual(removed, 6)
        self.assertEqual(self.service.video_yields("aaaaaaaaaaa").paths, ())
        reopened = library_db.LibraryDb(self.service.db.path)
        reopened.initialize()
        record = reopened.video("aaaaaaaaaaa")
        self.assertTrue(record.removed)
        self.assertFalse(record.downloaded)
        self.assertEqual(record.history.downloaded_at, initial.local.downloaded_at)
        self.assertIsNotNone(record.history.removed_at)
        self.assertEqual(record.playback.completed_at, watched_at)
        self.assertIsNone(reopened.video("bbbbbbbbbbb"))
        self.assertEqual(reopened.auto_pending_ids(self.channel.channel_id), set())
        self.assertEqual(reopened.recover_pipeline_jobs(), [])
        snapshot = self.feed.channel(self.channel.url, None, set())._replace(videos=[])
        self.assertEqual(reopened.store_snapshot(self.channel.channel_id, snapshot).pruned, 0)
        reopened.remove_channel(self.channel.channel_id)
        self.assertTrue(reopened.video("aaaaaaaaaaa").removed)
        self.assertIsNone(reopened.video("aaaaaaaaaaa").subscription_id)

    def test_redownload_restores_local_state_and_keeps_removal_and_watch_dates(self) -> None:
        """Let an explicit redownload restore files without erasing past lifecycle facts.

        Example: routine channel checks never automatically redownload a removed item.
        """

        self._download()
        self.service.db.record_playback(playback.make("aaaaaaaaaaa", 25, 100))
        self.service.remove_yields(self.service.video_yields("aaaaaaaaaaa"))
        removed = self.service.db.video("aaaaaaaaaaa")
        self.assertEqual(self.service.check_all(), 0)
        self.assertEqual(self.downloader.calls, ["aaaaaaaaaaa"])
        self.service.download("aaaaaaaaaaa")
        restored = self.service.db.video("aaaaaaaaaaa")
        self.assertTrue(restored.downloaded)
        self.assertFalse(restored.removed)
        self.assertEqual(restored.history.removed_at, removed.history.removed_at)
        self.assertEqual(restored.playback.position_seconds, 25)
        self.assertEqual(library_model.record_progress(restored, None).stage, progress.Stage.READY)

    def test_failed_or_incomplete_removal_does_not_claim_all_yields_removed(self) -> None:
        """Distinguish completed removal from errors or files outside the confirmed manifest.

        Example: a newly created log is retained and prevents a false Removed state.
        """

        self._download()
        manifest = self.service.video_yields("aaaaaaaaaaa")
        with mock.patch.object(library_yields.VideoYields, "remove", side_effect=PermissionError("locked")):
            with self.assertRaises(PermissionError):
                self.service.remove_yields(manifest)
        self.assertFalse(self.service.db.video("aaaaaaaaaaa").removed)
        log_dir = self.root / "logs"
        log_dir.mkdir()
        unexpected = log_dir / "aaaaaaaaaaa-new.log"
        unexpected.write_text("new log", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "yield files remain"):
            self.service.remove_yields(manifest)
        self.assertTrue(unexpected.exists())
        self.assertFalse(self.service.db.video("aaaaaaaaaaa").removed)
        self.assertIsNotNone(self.service.db.video("aaaaaaaaaaa").history.downloaded_at)

    def test_external_video_loss_preserves_history_without_claiming_yield_removal(self) -> None:
        """Remember a disappeared video while avoiding invented deliberate-removal facts.

        Example: an external deletion cannot make a watched record disposable.
        """

        downloaded = self._download()
        downloaded.local.path.unlink()
        self.service.db.set_setting("channel_published_after", "2026-01-01")
        self.service.scan_local()
        record = self.service.db.video("aaaaaaaaaaa")
        self.assertFalse(record.downloaded)
        self.assertFalse(record.removed)
        self.assertEqual(record.history.downloaded_at, downloaded.local.downloaded_at)
        self.assertIsNone(record.history.removed_at)

    def test_existing_catalog_seeds_downloads_and_protects_playback_only_history(self) -> None:
        """Migrate present downloads and retain older watch evidence without inventing dates.

        Example: old catalogs have no video_history table yet.
        """

        initial = self._download()
        self.service.db.record_playback(playback.make("bbbbbbbbbbb", 96, 100))
        with closing(sqlite3.connect(self.service.db.path)) as conn, conn:
            conn.execute("DROP TABLE video_history")
        reopened = library_db.LibraryDb(self.service.db.path)
        reopened.initialize()
        self.assertEqual(reopened.video("aaaaaaaaaaa").history.downloaded_at, initial.local.downloaded_at)
        reopened.reconcile_media([])
        self.assertEqual(reopened.prune_remote_before(200), 0)
        snapshot = self.feed.channel(self.channel.url, None, set())._replace(videos=[])
        self.assertEqual(reopened.store_snapshot(self.channel.channel_id, snapshot).pruned, 0)
        reopened.remove_channel(self.channel.channel_id)
        self.assertIsNotNone(reopened.video("aaaaaaaaaaa"))
        self.assertIsNotNone(reopened.video("bbbbbbbbbbb").playback.completed_at)

    def test_yields_only_removal_is_retained_without_inventing_a_download(self) -> None:
        """Keep a deliberately removed partial pipeline even if no video finished downloading.

        Example: an orphan subtitle can be removed without fabricating a download date.
        """

        directory = self.root / "subtitles"
        directory.mkdir()
        (directory / "aaaaaaaaaaa.srt").write_text("partial", encoding="utf-8")
        self.service.db.set_setting("channel_published_after", "2026-01-01")
        self.service.remove_yields(self.service.video_yields("aaaaaaaaaaa"))
        record = self.service.db.video("aaaaaaaaaaa")
        self.assertTrue(record.removed)
        self.assertIsNone(record.history.downloaded_at)
        self.assertIsNone(library_model.watched_progress(record))


class HistoryPresentationTests(unittest.TestCase):
    """Keep removed files, previous download dates, and watched progress visible.

    Example: Removed and Watched filters can both include the same retained record.
    """

    @classmethod
    def setUpClass(cls) -> None:
        """Create the shared Qt application for inspector assertions.

        Example: offscreen widgets never open a desktop window.
        """

        cls._app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_removed_and_watched_views_preserve_dates_and_counts(self) -> None:
        """Expose a removed download as historical instead of blank or failed.

        Example: an existing completion remains visible in the Watched column.
        """

        record = types.VideoRecord(
            fixtures.make_meta("aaaaaaaaaaa", "Removed"), None, 50, None, None,
            types.PlaybackState(96, 100, 150, 150),
            types.VideoHistory(100, 200, True),
        )
        model = library_model.VideoTableModel()
        model.set_records([record], {"aaaaaaaaaaa": None})
        proxy = library_model.VideoFilterModel()
        proxy.setSourceModel(model)
        self.assertEqual(model.progress_at(0).stage, progress.Stage.REMOVED)
        self.assertEqual(model.data(model.index(0, 1)), "✓ 96%")
        watched = model.data(model.index(0, 1), library_model.WATCHED_ROLE)
        self.assertEqual(watched.fraction, .96)
        self.assertTrue(watched.completed)
        self.assertEqual(model.data(model.index(0, 5)), library_model.format_timestamp(100))
        self.assertIn("removed", model.data(model.index(0, 0), QtCore.Qt.ItemDataRole.ToolTipRole))
        counts = proxy.facet_counts()
        self.assertEqual(counts[views.VideoView.REMOVED], 1)
        self.assertEqual(counts[views.VideoView.WATCHED], 1)
        self.assertEqual(counts[views.VideoView.AVAILABLE], 1)
        self.assertEqual(counts[views.VideoView.ON_DEVICE], 0)
        self.assertEqual(counts[views.VideoView.ISSUES], 0)
        for view in (views.VideoView.REMOVED, views.VideoView.WATCHED):
            proxy.set_view(view)
            self.assertEqual(proxy.rowCount(), 1)
        panel = library_widgets.DetailPanel()
        panel.set_record(record)
        self.assertIn("Last downloaded", panel._facts.text())
        self.assertIn("Video + yields removed", panel._facts.text())
        restored = record._replace(local=types.LocalMedia(Path("restored.mkv"), 300, 5))
        panel.set_record(restored)
        self.assertIn("Previously removed", panel._facts.text())


if __name__ == "__main__":
    unittest.main()
