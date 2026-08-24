"""Tests for catalog persistence and safe channel automation behavior.

Example: `python -m unittest tests.test_library`.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from yt_whisper_subs import library_db
from yt_whisper_subs import library_feed
from yt_whisper_subs import library_service
from yt_whisper_subs import library_types as types


def make_meta(video_id: str, title: str, published_at: int = 100) -> types.VideoMeta:
    """Build compact catalog metadata used by persistence and service tests.

    Example: `make_meta("aaaaaaaaaaa", "First")`.
    """

    ident = types.VideoIdentity(video_id, f"https://www.youtube.com/watch?v={video_id}", title)
    origin = types.VideoOrigin("Example channel", "UC-example", published_at)
    details = types.VideoDetails(90, 1234, f"Description for {title}", None, "not_live")
    return types.VideoMeta(ident, origin, details)


class FakeFeed:
    """Supply mutable channel snapshots without network access.

    Example: `feed.videos.append(meta)` simulates a later upload.
    """

    def __init__(self, videos: list[types.VideoMeta]) -> None:
        self.videos = videos
        self.fail = False

    def with_cookies(self, cookies: str | None) -> FakeFeed:
        """Keep the same fake strategy when GUI settings are reloaded.

        Example: `feed.with_cookies(None) is feed`.
        """

        del cookies
        return self

    def channel(self, url: str) -> types.ChannelSnapshot:
        """Return the current simulated uploads for one channel check.

        Example: `feed.channel(url).videos` mirrors `feed.videos`.
        """

        if self.fail:
            raise RuntimeError("simulated first-check failure")
        return types.ChannelSnapshot(url, "UC-example", "Example channel", list(self.videos))

    def video(self, url: str) -> types.VideoMeta:
        """Resolve one fake video by its ID-bearing URL.

        Example: `feed.video(meta.identity.url)` returns that metadata.
        """

        for meta in self.videos:
            if meta.identity.video_id in url:
                return meta
        raise RuntimeError("video missing from fake feed")

    def video_info(self, url: str) -> types.VideoInfo:
        """Return a JSON-serializable full-document stand-in for persistence.

        Example: `feed.video_info(url).document` becomes the test sidecar.
        """

        meta = self.video(url)
        document = {
            "id": meta.identity.video_id,
            "title": meta.identity.title,
            "channel": meta.origin.channel,
            "channel_id": meta.origin.channel_id,
            "timestamp": meta.origin.published_at,
        }
        return types.VideoInfo(meta, document)


class FakeDownloader:
    """Materialize a tiny media file instead of running Whisper and yt-dlp.

    Example: `downloader.calls` records automatic download IDs.
    """

    def __init__(self, out_dir: Path) -> None:
        self._out_dir = out_dir
        self.calls: list[str] = []

    def with_cookies(self, cookies: str | None) -> FakeDownloader:
        """Keep the same fake strategy when settings change.

        Example: `downloader.with_cookies("firefox") is downloader`.
        """

        del cookies
        return self

    def download(self, record: types.VideoRecord, report) -> None:
        """Write the canonical ID-named media file expected by local scanning.

        Example: `download(record, report)` makes the record playable.
        """

        video_id = record.meta.identity.video_id
        self.calls.append(video_id)
        video_dir = self._out_dir / "videos"
        video_dir.mkdir(parents=True, exist_ok=True)
        (video_dir / f"{video_id}.mkv").write_bytes(b"video")
        report(f"Created {video_id}")


class PipelineDownloaderTests(unittest.TestCase):
    """Preserve the pipeline's actionable error at the GUI boundary.

    Example: `PipelineDownloaderTests("test_download_reports_root_error")`.
    """

    @mock.patch("yt_whisper_subs.library_service.subprocess.Popen")
    def test_download_reports_root_error(self, popen: mock.Mock) -> None:
        """Show yt-dlp's HTTP failure instead of a generic wrapper message.

        Example: the native error dialog includes `HTTP Error 403`.
        """

        process = popen.return_value
        process.stdout = io.StringIO(
            "ERROR: unable to download video data: HTTP Error 403: Forbidden\n"
            "error: ERROR: unable to download video data: HTTP Error 403: Forbidden\n"
        )
        process.wait.return_value = 1
        record = types.VideoRecord(make_meta("aaaaaaaaaaa", "Example"), None, 0, None, None)
        downloader = library_service.PipelineDownloader(Path("python"), Path("output"))

        with self.assertRaisesRegex(RuntimeError, "HTTP Error 403"):
            downloader.download(record, lambda _message: None)


class LibraryDbTests(unittest.TestCase):
    """Cover subscription, metadata, local-media, and removal contracts.

    Example: `LibraryDbTests("test_channel_removal_keeps_download")`.
    """

    def test_snapshot_tracks_new_ids_and_download_state(self) -> None:
        """Return only unseen IDs while keeping remote and local state separate.

        Example: a second snapshot identifies only its added video.
        """

        with tempfile.TemporaryDirectory() as tmp:
            db = library_db.LibraryDb(Path(tmp) / "catalog.sqlite3")
            db.initialize()
            channel = db.add_channel("https://www.youtube.com/@example/videos", True)
            first = make_meta("aaaaaaaaaaa", "First")
            snapshot = types.ChannelSnapshot(channel.url, "UC-example", "Example", [first])
            self.assertEqual(db.store_snapshot(channel.channel_id, snapshot), ["aaaaaaaaaaa"])
            second = make_meta("bbbbbbbbbbb", "Second", 200)
            snapshot = snapshot._replace(videos=[second, first])
            self.assertEqual(db.store_snapshot(channel.channel_id, snapshot), ["bbbbbbbbbbb"])

            local_path = Path(tmp) / "bbbbbbbbbbb.mkv"
            local_path.write_bytes(b"video")
            local = types.LocalMedia(local_path, 300, local_path.stat().st_size)
            db.reconcile_media([types.ScannedMedia(second, local, True)])
            self.assertTrue(db.video("bbbbbbbbbbb").downloaded)
            self.assertFalse(db.video("aaaaaaaaaaa").downloaded)
            self.assertEqual(db.stats(), types.LibraryStats(2, 1, 1, 1))

    def test_channel_removal_keeps_download(self) -> None:
        """Delete unneeded remote history but retain locally downloaded rows.

        Example: stopping tracking never hides an existing media file.
        """

        with tempfile.TemporaryDirectory() as tmp:
            db = library_db.LibraryDb(Path(tmp) / "catalog.sqlite3")
            db.initialize()
            channel = db.add_channel("https://www.youtube.com/@example/videos", False)
            local_meta = make_meta("aaaaaaaaaaa", "Local")
            pending_meta = make_meta("bbbbbbbbbbb", "Pending")
            snapshot = types.ChannelSnapshot(channel.url, "UC-example", "Example", [local_meta, pending_meta])
            db.store_snapshot(channel.channel_id, snapshot)
            path = Path(tmp) / "aaaaaaaaaaa.mkv"
            path.write_bytes(b"video")
            db.reconcile_media([types.ScannedMedia(local_meta, types.LocalMedia(path, 100, 5), True)])

            db.remove_channel(channel.channel_id)

            self.assertIsNotNone(db.video("aaaaaaaaaaa"))
            self.assertIsNone(db.video("bbbbbbbbbbb"))
            self.assertEqual(db.stats().channels, 0)


class LibraryServiceTests(unittest.TestCase):
    """Cover baseline safety and future-only automatic download behavior.

    Example: `LibraryServiceTests("test_auto_download_skips_initial_history")`.
    """

    def test_auto_download_skips_initial_history(self) -> None:
        """Never download a backlog, then download a video found on a later check.

        Example: adding an auto channel with one old video produces no call.
        """

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            first = make_meta("aaaaaaaaaaa", "First")
            feed = FakeFeed([first])
            downloader = FakeDownloader(out_dir)
            paths = {"python": Path("python")}
            service = library_service.LibraryService(
                out_dir,
                paths,
                feed=feed,
                downloader=downloader,
            )
            service.add_channel("@example", True)
            self.assertEqual(downloader.calls, [])

            second = make_meta("bbbbbbbbbbb", "Second", 200)
            feed.videos.insert(0, second)
            self.assertEqual(service.check_all(), 1)

            self.assertEqual(downloader.calls, ["bbbbbbbbbbb"])
            self.assertTrue(service.db.video("bbbbbbbbbbb").downloaded)
            self.assertFalse(service.db.video("aaaaaaaaaaa").downloaded)

    def test_scan_ingests_metadata_sidecar(self) -> None:
        """Use preserved info JSON to title and date an existing local download.

        Example: startup scanning needs no network when a sidecar exists.
        """

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            video_dir = out_dir / "videos"
            metadata_dir = out_dir / "metadata"
            video_dir.mkdir()
            metadata_dir.mkdir()
            video_id = "aaaaaaaaaaa"
            (video_dir / f"{video_id}.mkv").write_bytes(b"video")
            info = {
                "id": video_id,
                "title": "Preserved title",
                "channel": "Preserved channel",
                "channel_id": "UC-example",
                "timestamp": 123,
                "duration": 90,
            }
            (metadata_dir / f"{video_id}.info.json").write_text(json.dumps(info), encoding="utf-8")
            feed = FakeFeed([])
            service = library_service.LibraryService(
                out_dir,
                {"python": Path("python")},
                feed=feed,
                downloader=FakeDownloader(out_dir),
            )

            service.scan_local()

            record = service.db.video(video_id)
            self.assertEqual(record.meta.identity.title, "Preserved title")
            self.assertEqual(record.meta.origin.channel, "Preserved channel")
            self.assertEqual(service.db.missing_metadata_ids(12), [])

    def test_failed_initial_check_does_not_arm_backlog_download(self) -> None:
        """Keep baseline incomplete until one channel snapshot succeeds.

        Example: a network failure followed by retry still skips old uploads.
        """

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            feed = FakeFeed([make_meta("aaaaaaaaaaa", "Existing")])
            feed.fail = True
            downloader = FakeDownloader(out_dir)
            service = library_service.LibraryService(
                out_dir,
                {"python": Path("python")},
                feed=feed,
                downloader=downloader,
            )
            with self.assertRaisesRegex(RuntimeError, "simulated"):
                service.add_channel("@example", True)
            channel = service.db.channels()[0]
            self.assertIsNone(channel.baseline_at)
            self.assertIsNotNone(channel.checked_at)

            feed.fail = False
            service.check_all()

            self.assertEqual(downloader.calls, [])
            self.assertIsNotNone(service.db.channels()[0].baseline_at)


class LibraryFeedTests(unittest.TestCase):
    """Cover channel normalization and yt-dlp field mapping.

    Example: `LibraryFeedTests("test_video_meta_maps_useful_fields")`.
    """

    def test_normalize_channel_urls(self) -> None:
        """Route handles and base channel URLs to the videos tab.

        Example: `@example` becomes a canonical HTTPS URL.
        """

        self.assertEqual(
            library_feed.normalize_channel_url("@example"),
            "https://www.youtube.com/@example/videos",
        )
        self.assertEqual(
            library_feed.normalize_channel_url("https://youtube.com/channel/UC123"),
            "https://www.youtube.com/channel/UC123/videos",
        )
        self.assertEqual(
            library_feed.channel_tab_urls("@example"),
            [
                "https://www.youtube.com/@example/videos",
                "https://www.youtube.com/@example/shorts",
                "https://www.youtube.com/@example/streams",
            ],
        )

    def test_video_meta_maps_useful_fields(self) -> None:
        """Preserve title, channel, upload time, duration, views, and description.

        Example: full video JSON creates nested catalog metadata.
        """

        info = {
            "id": "aaaaaaaaaaa",
            "title": "A title",
            "channel": "A channel",
            "channel_id": "UC123",
            "timestamp": 123,
            "duration": 90.5,
            "view_count": 42,
            "description": "Useful details",
            "thumbnail": "https://example.test/thumb.jpg",
            "live_status": "not_live",
        }
        meta = library_feed.video_meta(info)
        self.assertEqual(meta.identity.title, "A title")
        self.assertEqual(meta.origin, types.VideoOrigin("A channel", "UC123", 123))
        self.assertEqual(meta.details.duration, 90.5)
        self.assertEqual(meta.details.view_count, 42)

    def test_rss_timestamp_parser(self) -> None:
        """Parse the exact RFC 3339 timestamps returned by channel feeds.

        Example: recent flat channel entries use this publication time.
        """

        value = library_feed._rss_timestamp("2026-08-18T12:30:00+00:00")
        self.assertEqual(value, 1787056200)


if __name__ == "__main__":
    unittest.main()
