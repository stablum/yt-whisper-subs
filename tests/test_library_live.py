"""Check upload eligibility without weakening confirmed-stream download guards.

Example: `python -m unittest tests.test_library_live`.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PySide6 import QtCore

from tests import test_library as fixtures
from yt_whisper_subs import library_feed
from yt_whisper_subs import library_model
from yt_whisper_subs import library_service
from yt_whisper_subs import library_types as types
from yt_whisper_subs import pipeline_progress as progress


class StreamPolicyTests(unittest.TestCase):
    """Exercise persisted status transitions and the actual automation path.

    Example: a stream missing its status stays pending across repeated scans.
    """

    def test_missing_stream_status_stays_pending_across_refresh_and_restart(self) -> None:
        """Keep a known stream blocked despite a dated, incomplete listing.

        Example: two channel scans cannot release an unconfirmed broadcast.
        """

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            feed = fixtures.FakeFeed([])
            downloader = fixtures.FakeDownloader(root)
            service = library_service.LibraryService(
                root, {"python": Path("python")}, feed=feed, downloader=downloader
            )
            channel = service.track_channel("@example", True)
            service.initialize_channel(channel.channel_id)
            video_id = "aaaaaaaaaaa"
            live = fixtures.with_live_status(fixtures.make_meta(video_id, "Stream"), "is_live")
            feed.videos = [live]
            self.assertEqual(service.check_all(), 0)

            feed.videos = [fixtures.with_live_status(live, None)]
            self.assertEqual(service.check_all(), 0)
            service = library_service.LibraryService(
                root, {"python": Path("python")}, feed=feed, downloader=downloader
            )
            self.assertEqual(service.check_all(), 0)
            record = service.db.video(video_id)
            self.assertEqual(record.meta.details.live_status, types.LIVE_UNKNOWN)
            self.assertEqual(service.db.auto_pending_ids(channel.channel_id), {video_id})
            self.assertEqual(downloader.calls, [])
            self.assertEqual(feed.video_info_calls, 0)

            model = library_model.VideoTableModel()
            model.set_records([record])
            cell = model.index(0, library_model.PIPELINE_COLUMN)
            self.assertEqual(cell.data(), "Availability unconfirmed")
            self.assertIn("Double-click to check", cell.data(QtCore.Qt.ItemDataRole.ToolTipRole))
            feed.videos = [fixtures.with_live_status(live, "was_live")]
            self.assertEqual(service.check_all(), 1)
            self.assertEqual(downloader.calls, [video_id])

    def test_refresh_repairs_false_upload_warning_without_downloading_baseline(self) -> None:
        """Clear unsupported old warnings using a fresh ordinary-upload listing.

        Example: an existing AT5 upload becomes Available without a full lookup.
        """

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video_id = "aaaaaaaaaaa"
            upload = fixtures.with_live_status(fixtures.make_meta(video_id, "Upload"), None)
            feed = fixtures.FakeFeed([upload])
            downloader = fixtures.FakeDownloader(root)
            service = library_service.LibraryService(
                root, {"python": Path("python")}, feed=feed, downloader=downloader
            )
            channel = service.track_channel("@example", True)
            service.initialize_channel(channel.channel_id)
            # Reproduce the original new-upload classification: no stream/probe timer.
            service.db.set_live_status(video_id, types.LIVE_UNKNOWN)
            feed.complete = False
            self.assertEqual(service.check_all(), 0)
            self.assertEqual(service.db.video(video_id).meta.details.live_status, types.LIVE_UNKNOWN)
            feed.complete = True
            self.assertEqual(service.check_all(), 0)
            record = service.db.video(video_id)
            self.assertIsNone(record.meta.details.live_status)
            self.assertEqual(library_model.record_progress(record).stage, progress.Stage.AVAILABLE)
            self.assertEqual(feed.video_info_calls, 0)
            self.assertEqual(downloader.calls, [])
            service.download(video_id)
            self.assertEqual(feed.video_info_calls, 0)
            self.assertEqual(downloader.calls, [video_id])

    def test_repaired_upload_releases_existing_auto_download_pending(self) -> None:
        """Recover a new upload wrongly held in the automatic-download queue.

        Example: a missing flat status no longer strands an eligible upload.
        """

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            feed = fixtures.FakeFeed([])
            downloader = fixtures.FakeDownloader(root)
            service = library_service.LibraryService(
                root, {"python": Path("python")}, feed=feed, downloader=downloader
            )
            channel = service.track_channel("@example", True)
            service.initialize_channel(channel.channel_id)
            video_id = "aaaaaaaaaaa"
            upload = fixtures.with_live_status(fixtures.make_meta(video_id, "Upload"), None)
            service.db.upsert_video(upload, channel.channel_id)
            service.db.set_live_status(video_id, types.LIVE_UNKNOWN)
            service.db.set_auto_pending((video_id,), True)
            feed.videos = [upload]
            self.assertEqual(service.check_all(), 1)
            self.assertEqual(downloader.calls, [video_id])
            self.assertEqual(service.db.auto_pending_ids(channel.channel_id), set())
            self.assertEqual(feed.video_info_calls, 0)

    def test_full_lookup_missing_status_preserves_only_existing_stream_guard(self) -> None:
        """Do not create a stream claim from an incomplete ordinary-video lookup.

        Example: unknown data for a known live video still prevents downloading.
        """

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            upload = fixtures.with_live_status(fixtures.make_meta("aaaaaaaaaaa", "Upload"), None)
            stream = fixtures.with_live_status(fixtures.make_meta("bbbbbbbbbbb", "Stream"), "is_live")
            service = library_service.LibraryService(
                root, {"python": Path("python")}, feed=fixtures.FakeFeed([])
            )
            service.db.upsert_video(upload)
            service.db.upsert_video(stream)
            for meta in (upload, fixtures.with_live_status(stream, None)):
                service._save_video_info(types.VideoInfo(meta, {"id": meta.identity.video_id}))
            self.assertIsNone(service.db.video("aaaaaaaaaaa").meta.details.live_status)
            self.assertEqual(service.db.video("bbbbbbbbbbb").meta.details.live_status, types.LIVE_UNKNOWN)

    def test_stream_tab_membership_blocks_missing_status_and_duplicate_upload(self) -> None:
        """Use stream-tab evidence even when a duplicate Videos row has no badge.

        Example: an undetermined replay cannot masquerade as an ordinary upload.
        """

        feed = library_feed.YtDlpFeed(Path("python"))
        videos = {"entries": [{"id": "aaaaaaaaaaa"}, {"id": "bbbbbbbbbbb"}]}
        streams = {"entries": [{"id": "bbbbbbbbbbb"}, {"id": "ccccccccccc", "live_status": "was_live"}]}
        policy = library_feed.ChannelScanPolicy(50, 500, None)
        with mock.patch.object(feed, "_channel_tab", side_effect=[videos, streams]):
            snapshot = feed.channel("@example", policy)
        statuses = {meta.identity.video_id: meta.details.live_status for meta in snapshot.videos}
        self.assertEqual(statuses, {"aaaaaaaaaaa": None, "bbbbbbbbbbb": types.LIVE_UNKNOWN, "ccccccccccc": "was_live"})


if __name__ == "__main__":
    unittest.main()
