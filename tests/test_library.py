"""Tests for catalog persistence and safe channel automation behavior.

Example: `python -m unittest tests.test_library`.
"""

from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from yt_whisper_subs import library_db
from yt_whisper_subs import library_feed
from yt_whisper_subs import library_model
from yt_whisper_subs import library_service
from yt_whisper_subs import library_types as types
from yt_whisper_subs import playback_progress
from yt_whisper_subs import pipeline_progress as progress


def make_meta(video_id: str, title: str, published_at: int | None = 100) -> types.VideoMeta:
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
        self.complete = True
        self.video_fail = False
        self.channel_calls = 0

    def with_cookies(self, cookies: str | None) -> FakeFeed:
        """Keep the same fake strategy when GUI settings are reloaded.

        Example: `feed.with_cookies(None) is feed`.
        """

        del cookies
        return self

    def channel(
        self,
        url: str,
        policy: library_feed.ChannelScanPolicy,
        known_ids: set[str],
    ) -> types.ChannelSnapshot:
        """Return the current simulated uploads for one channel check.

        Example: `feed.channel(url, policy, ids).videos` mirrors `feed.videos`.
        """

        del policy, known_ids
        self.channel_calls += 1
        if self.fail:
            raise RuntimeError("simulated first-check failure")
        return types.ChannelSnapshot(
            url,
            "UC-example",
            "Example channel",
            list(self.videos),
            self.complete,
        )

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

        if self.video_fail:
            raise RuntimeError("simulated metadata failure")
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
        record = types.VideoRecord(make_meta("aaaaaaaaaaa", "Example"), None, 0, None, None, None)
        downloader = library_service.PipelineDownloader(Path("python"), Path("output"))

        reports: list[str] = []
        with self.assertRaisesRegex(RuntimeError, "HTTP Error 403"):
            downloader.download(record, reports.append)

        child_env = popen.call_args.kwargs["env"]
        cmd = [str(part) for part in popen.call_args.args[0]]
        self.assertIn("--chapters", cmd)
        self.assertEqual(child_env[progress.ENV_VIDEO_ID], "aaaaaaaaaaa")
        updates = [update for message in reports if (update := progress.parse(message))]
        self.assertEqual(updates[0].stage, progress.Stage.QUEUED)
        self.assertEqual(updates[-1].stage, progress.Stage.FAILED)
        self.assertIn("HTTP Error 403", updates[-1].label)


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
            channel = db.add_channel("https://www.youtube.com/@example/videos", "@example", True)
            first = make_meta("aaaaaaaaaaa", "First")
            snapshot = types.ChannelSnapshot(channel.url, "UC-example", "Example", [first], True)
            self.assertEqual(
                db.store_snapshot(channel.channel_id, snapshot).new_ids,
                ["aaaaaaaaaaa"],
            )
            second = make_meta("bbbbbbbbbbb", "Second", 200)
            snapshot = snapshot._replace(videos=[second, first])
            self.assertEqual(
                db.store_snapshot(channel.channel_id, snapshot).new_ids,
                ["bbbbbbbbbbb"],
            )

            local_path = Path(tmp) / "bbbbbbbbbbb.mkv"
            local_path.write_bytes(b"video")
            local = types.LocalMedia(local_path, 300, local_path.stat().st_size)
            db.reconcile_media([types.ScannedMedia(second, local, True)])
            self.assertTrue(db.video("bbbbbbbbbbb").downloaded)
            self.assertFalse(db.video("aaaaaaaaaaa").downloaded)
            self.assertEqual(len(db.videos()), 2)
            self.assertEqual(len(db.channels()), 1)

    def test_channel_removal_keeps_download(self) -> None:
        """Delete unneeded remote history but retain locally downloaded rows.

        Example: stopping tracking never hides an existing media file.
        """

        with tempfile.TemporaryDirectory() as tmp:
            db = library_db.LibraryDb(Path(tmp) / "catalog.sqlite3")
            db.initialize()
            channel = db.add_channel("https://www.youtube.com/@example/videos", "@example", False)
            local_meta = make_meta("aaaaaaaaaaa", "Local")
            pending_meta = make_meta("bbbbbbbbbbb", "Pending")
            snapshot = types.ChannelSnapshot(
                channel.url,
                "UC-example",
                "Example",
                [local_meta, pending_meta],
                True,
            )
            db.store_snapshot(channel.channel_id, snapshot)
            path = Path(tmp) / "aaaaaaaaaaa.mkv"
            path.write_bytes(b"video")
            db.reconcile_media([types.ScannedMedia(local_meta, types.LocalMedia(path, 100, 5), True)])

            db.remove_channel(channel.channel_id)

            self.assertIsNotNone(db.video("aaaaaaaaaaa"))
            self.assertIsNone(db.video("bbbbbbbbbbb"))
            self.assertEqual(db.channels(), [])

    def test_bounded_snapshot_prunes_only_remote_history(self) -> None:
        """Remove entries outside a complete slice while preserving downloads.

        Example: an old local video survives when only the newest remote row remains.
        """

        with tempfile.TemporaryDirectory() as tmp:
            db = library_db.LibraryDb(Path(tmp) / "catalog.sqlite3")
            db.initialize()
            channel = db.add_channel("https://www.youtube.com/@example/videos", "@example", False)
            local_old = make_meta("aaaaaaaaaaa", "Local old", 10)
            remote_old = make_meta("bbbbbbbbbbb", "Remote old", 20)
            recent = make_meta("ccccccccccc", "Recent", 30)
            snapshot = types.ChannelSnapshot(
                channel.url,
                "UC-example",
                "Example",
                [recent, remote_old, local_old],
                True,
            )
            db.store_snapshot(channel.channel_id, snapshot)
            path = Path(tmp) / "aaaaaaaaaaa.mkv"
            path.write_bytes(b"video")
            local = types.LocalMedia(path, 40, path.stat().st_size)
            db.reconcile_media([types.ScannedMedia(local_old, local, True)])

            result = db.store_snapshot(
                channel.channel_id,
                snapshot._replace(videos=[recent]),
            )

            self.assertEqual(result.pruned, 1)
            self.assertIsNone(db.video("bbbbbbbbbbb"))
            self.assertIsNotNone(db.video("aaaaaaaaaaa"))
            self.assertTrue(db.video("aaaaaaaaaaa").downloaded)

    def test_incomplete_snapshot_does_not_prune_and_cutoff_keeps_downloads(self) -> None:
        """Avoid destructive sync after a failed tab and protect local old rows.

        Example: a Streams extraction failure preserves history until recovery.
        """

        with tempfile.TemporaryDirectory() as tmp:
            db = library_db.LibraryDb(Path(tmp) / "catalog.sqlite3")
            db.initialize()
            channel = db.add_channel("https://www.youtube.com/@example/videos", "@example", False)
            local_old = make_meta("aaaaaaaaaaa", "Local old", 10)
            remote_old = make_meta("bbbbbbbbbbb", "Remote old", 20)
            snapshot = types.ChannelSnapshot(
                channel.url,
                "UC-example",
                "Example",
                [remote_old, local_old],
                True,
            )
            db.store_snapshot(channel.channel_id, snapshot)
            path = Path(tmp) / "aaaaaaaaaaa.mkv"
            path.write_bytes(b"video")
            local = types.LocalMedia(path, 30, path.stat().st_size)
            db.reconcile_media([types.ScannedMedia(local_old, local, True)])

            result = db.store_snapshot(
                channel.channel_id,
                snapshot._replace(videos=[local_old], complete=False),
            )
            removed = db.prune_remote_before(25)

            self.assertEqual(result.pruned, 0)
            self.assertEqual(removed, 1)
            self.assertIsNone(db.video("bbbbbbbbbbb"))
            self.assertTrue(db.video("aaaaaaaaaaa").downloaded)

    def test_metadata_queue_is_fair_and_cools_down_attempts(self) -> None:
        """Prefer untouched rows and suppress attempted rows until retry time.

        Example: one unavailable video cannot starve the remaining backlog.
        """

        with tempfile.TemporaryDirectory() as tmp:
            db = library_db.LibraryDb(Path(tmp) / "catalog.sqlite3")
            db.initialize()
            channel = db.add_channel("https://www.youtube.com/@example/videos", "@example", False)
            first = make_meta("aaaaaaaaaaa", "First", None)
            second = make_meta("bbbbbbbbbbb", "Second", None)
            snapshot = types.ChannelSnapshot(
                channel.url,
                "UC-example",
                "Example",
                [first, second],
                True,
            )
            db.store_snapshot(channel.channel_id, snapshot)

            attempted_id = db.metadata_backfill_ids(1, retry_before=1_000)[0]
            db.mark_metadata_attempt(attempted_id, attempted_at=1_000)
            next_id = db.metadata_backfill_ids(1, retry_before=999)[0]
            self.assertNotEqual(next_id, attempted_id)
            db.mark_metadata_attempt(next_id, attempted_at=1_001)

            self.assertEqual(db.metadata_backfill_ids(1, retry_before=999), [])
            self.assertEqual(db.metadata_backfill_ids(1, retry_before=1_000), [attempted_id])
            self.assertEqual(db.metadata_backlog_count(), 2)

    def test_initialize_migrates_an_existing_catalog(self) -> None:
        """Add queue and playback state without discarding an existing catalog.

        Example: version 0.2.6 databases open directly in version 0.3.0.
        """

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog.sqlite3"
            legacy_schema = library_db._SCHEMA.replace(
                "    metadata_attempted_at INTEGER,\n",
                "",
            )
            legacy_schema = legacy_schema.replace(
                """
CREATE TABLE IF NOT EXISTS playback (
    video_id TEXT PRIMARY KEY REFERENCES videos(video_id) ON DELETE CASCADE,
    position_seconds REAL NOT NULL DEFAULT 0,
    duration_seconds REAL,
    completed_at INTEGER,
    updated_at INTEGER NOT NULL
);
""",
                "",
            )
            with closing(sqlite3.connect(path)) as conn:
                conn.executescript(legacy_schema)
                conn.commit()

            db = library_db.LibraryDb(path)
            db.initialize()

            with closing(sqlite3.connect(path)) as conn:
                columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(videos)")}
                tables = {
                    str(row[0])
                    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
            self.assertIn("metadata_attempted_at", columns)
            self.assertIn("playback", tables)

    def test_playback_progress_and_completion_are_durable_and_monotonic(self) -> None:
        """Keep furthest position and never erase a confirmed watched state.

        Example: replaying from the beginning leaves a completed video at 100%.
        """

        with tempfile.TemporaryDirectory() as tmp:
            db = library_db.LibraryDb(Path(tmp) / "catalog.sqlite3")
            db.initialize()
            channel = db.add_channel("https://www.youtube.com/@example/videos", "@example", False)
            meta = make_meta("aaaaaaaaaaa", "First")
            snapshot = types.ChannelSnapshot(channel.url, "UC-example", "Example", [meta], True)
            db.store_snapshot(channel.channel_id, snapshot)

            db.record_playback(playback_progress.make("aaaaaaaaaaa", 40, 100))
            db.record_playback(playback_progress.make("aaaaaaaaaaa", 20, 100))
            partial = db.video("aaaaaaaaaaa").playback
            self.assertEqual(partial.position_seconds, 40)
            self.assertIsNone(partial.completed_at)

            db.record_playback(playback_progress.make("aaaaaaaaaaa", 99, 100, True))
            db.record_playback(playback_progress.make("aaaaaaaaaaa", 5, 100))
            completed = db.video("aaaaaaaaaaa").playback
            self.assertEqual(completed.position_seconds, 100)
            self.assertIsNotNone(completed.completed_at)


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
            channel = service.track_channel("@example", True)
            service.initialize_channel(channel.channel_id)
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
            self.assertEqual(service.db.metadata_backlog_count(), 0)

    def test_metadata_backfill_attempts_only_one_video(self) -> None:
        """Hydrate one missing date per call and leave the rest queued.

        Example: the GUI's minute timer cannot trigger a request burst.
        """

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            first = make_meta("aaaaaaaaaaa", "First", None)
            second = make_meta("bbbbbbbbbbb", "Second", None)
            feed = FakeFeed([first, second])
            service = library_service.LibraryService(
                out_dir,
                {"python": Path("python")},
                feed=feed,
                downloader=FakeDownloader(out_dir),
            )
            channel = service.track_channel("@example", False)
            service.initialize_channel(channel.channel_id)
            feed.videos = [
                make_meta("aaaaaaaaaaa", "First", 100),
                make_meta("bbbbbbbbbbb", "Second", 200),
            ]

            result = service.backfill_metadata()

            self.assertTrue(result.attempted)
            self.assertTrue(result.completed)
            self.assertEqual(result.remaining, 1)
            self.assertEqual(service.db.metadata_backlog_count(), 1)

    def test_failed_metadata_lookup_is_deferred_without_pipeline_error(self) -> None:
        """Cooldown an unavailable lookup without marking the download failed.

        Example: a temporary YouTube refusal stays in the trace and queue.
        """

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            feed = FakeFeed([make_meta("aaaaaaaaaaa", "First", None)])
            service = library_service.LibraryService(
                out_dir,
                {"python": Path("python")},
                feed=feed,
                downloader=FakeDownloader(out_dir),
            )
            channel = service.track_channel("@example", False)
            service.initialize_channel(channel.channel_id)
            feed.video_fail = True

            result = service.backfill_metadata()
            cooled_down = service.backfill_metadata()

            self.assertTrue(result.attempted)
            self.assertFalse(result.completed)
            self.assertFalse(cooled_down.attempted)
            self.assertIsNone(service.db.video("aaaaaaaaaaa").download_error)

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
            channel = service.track_channel("@example", True)
            self.assertEqual(channel.title, "@example")
            self.assertEqual(feed.channel_calls, 0)
            with self.assertRaisesRegex(RuntimeError, "simulated"):
                service.initialize_channel(channel.channel_id)
            self.assertEqual(feed.channel_calls, 1)
            channel = service.db.channels()[0]
            self.assertIsNone(channel.baseline_at)
            self.assertIsNotNone(channel.checked_at)

            feed.fail = False
            service.check_all()

            self.assertEqual(downloader.calls, [])
            self.assertIsNotNone(service.db.channels()[0].baseline_at)

    def test_partial_refresh_is_visible_and_cannot_establish_baseline(self) -> None:
        """Keep incomplete discovery safe and explain why history remains.

        Example: a real Streams failure records a warning until a full retry.
        """

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            old = make_meta("aaaaaaaaaaa", "Existing")
            feed = FakeFeed([old])
            feed.complete = False
            downloader = FakeDownloader(out_dir)
            service = library_service.LibraryService(
                out_dir,
                {"python": Path("python")},
                feed=feed,
                downloader=downloader,
            )
            channel = service.track_channel("@example", True)

            service.initialize_channel(channel.channel_id)

            partial = service.db.channel(channel.channel_id)
            self.assertIsNotNone(partial)
            self.assertIsNone(partial.baseline_at)
            self.assertIn("Partial refresh", partial.last_error)

            feed.complete = True
            feed.videos.insert(0, make_meta("bbbbbbbbbbb", "Also existing", 200))
            service.check_all()

            recovered = service.db.channel(channel.channel_id)
            self.assertIsNotNone(recovered.baseline_at)
            self.assertIsNone(recovered.last_error)
            self.assertEqual(downloader.calls, [])


class LibraryModelTests(unittest.TestCase):
    """Keep watched-column wording aligned with durable completion semantics.

    Example: `LibraryModelTests("test_100_percent_requires_completion")`.
    """

    def test_100_percent_requires_completion(self) -> None:
        """Cap position-derived display at 99 until an EOF timestamp exists.

        Example: reaching duration numerically is not itself completion proof.
        """

        local = types.LocalMedia(Path("video.mkv"), 100, 5)
        partial = types.VideoRecord(
            make_meta("aaaaaaaaaaa", "First"),
            None,
            100,
            local,
            None,
            types.PlaybackState(100, 100, None, 200),
        )

        partial_view = library_model.watched_progress(partial)
        completed_view = library_model.watched_progress(
            partial._replace(playback=types.PlaybackState(100, 100, 300, 300))
        )

        self.assertEqual(partial_view.fraction, 0.99)
        self.assertEqual(partial_view.label, "99%")
        self.assertEqual(completed_view.fraction, 1.0)
        self.assertEqual(completed_view.label, "✓ 100%")

    def test_smart_views_compose_search_counts_and_live_progress(self) -> None:
        """Keep smart-view facets useful inside search and live playback changes.

        Example: an Unwatched row moves to Continue after its first mpv update.
        """

        local = types.LocalMedia(Path("video.mkv"), 100, 5)
        records = [
            types.VideoRecord(make_meta("aaaaaaaaaaa", "Remote"), None, 100, None, None, None),
            types.VideoRecord(make_meta("bbbbbbbbbbb", "Fresh"), None, 100, local, None, None),
            types.VideoRecord(
                make_meta("ccccccccccc", "Partial"),
                None,
                100,
                local,
                None,
                types.PlaybackState(25, 100, None, 200),
            ),
            types.VideoRecord(
                make_meta("ddddddddddd", "Finished"),
                None,
                100,
                local,
                None,
                types.PlaybackState(100, 100, 300, 300),
            ),
            types.VideoRecord(make_meta("eeeeeeeeeee", "Broken"), None, 100, None, "HTTP 403", None),
        ]
        model = library_model.VideoTableModel()
        model.set_records(records)
        proxy = library_model.VideoFilterModel()
        proxy.setSourceModel(model)

        counts = proxy.facet_counts()
        self.assertEqual(counts[library_model.VideoView.ALL], 5)
        self.assertEqual(counts[library_model.VideoView.ON_DEVICE], 3)
        self.assertEqual(counts[library_model.VideoView.AVAILABLE], 2)
        self.assertEqual(counts[library_model.VideoView.UNWATCHED], 1)
        self.assertEqual(counts[library_model.VideoView.CONTINUE], 1)
        self.assertEqual(counts[library_model.VideoView.WATCHED], 1)
        self.assertEqual(counts[library_model.VideoView.ISSUES], 1)

        proxy.set_view(library_model.VideoView.UNWATCHED)
        self.assertEqual(proxy.rowCount(), 1)
        model.set_watched_progress(playback_progress.make("bbbbbbbbbbb", 10, 100))
        self.assertEqual(proxy.rowCount(), 0)
        self.assertEqual(proxy.facet_counts()[library_model.VideoView.CONTINUE], 2)

        proxy.set_view(library_model.VideoView.CONTINUE)
        proxy.set_search("partial")
        self.assertEqual(proxy.rowCount(), 1)
        searched = proxy.facet_counts()
        self.assertEqual(searched[library_model.VideoView.ALL], 1)
        self.assertEqual(searched[library_model.VideoView.CONTINUE], 1)

    def test_smart_view_keys_restore_safely(self) -> None:
        """Persist human-independent view keys and reject stale settings safely.

        Example: an unknown key opens All instead of an empty library.
        """

        self.assertEqual(
            library_model.VideoView.from_key("on_device"),
            library_model.VideoView.ON_DEVICE,
        )
        self.assertEqual(
            library_model.VideoView.from_key("removed-view"),
            library_model.VideoView.ALL,
        )


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
                "https://www.youtube.com/@example/streams",
            ],
        )
        self.assertEqual(library_feed.channel_placeholder("@ruis"), "@ruis")

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

    def test_recent_tab_expands_only_until_known_overlap(self) -> None:
        """Bound ordinary checks but bridge a gap larger than the normal slice.

        Example: a known ID at position 76 expands a 50-entry request to 100.
        """

        feed = library_feed.YtDlpFeed(Path("python"))
        entries = [{"id": f"{idx:011d}"} for idx in range(80)]

        def response(args: list[str]) -> dict[str, object]:
            """Return as many fake playlist entries as the yt-dlp limit requests.

            Example: `--playlist-end 50` returns the first 50 simulated IDs.
            """

            limit = int(args[args.index("--playlist-end") + 1])
            return {"entries": entries[:limit]}

        policy = library_feed.ChannelScanPolicy(50, 200, None)
        known_id = str(entries[75]["id"])
        with mock.patch.object(feed, "_json", side_effect=response) as run:
            info = feed._channel_tab("https://example.test/videos", policy, {known_id})

        limits = [int(call.args[0][2]) for call in run.call_args_list]
        self.assertEqual(limits, [50, 100])
        self.assertEqual(len(info["entries"]), 80)

    def test_initial_tab_uses_one_bounded_request(self) -> None:
        """Keep a new subscription light when no overlap must be recovered.

        Example: initial discovery asks yt-dlp for only the configured 50 rows.
        """

        feed = library_feed.YtDlpFeed(Path("python"))
        info = {"entries": [{"id": "aaaaaaaaaaa"}]}
        policy = library_feed.ChannelScanPolicy(50, 500, None)
        with mock.patch.object(feed, "_json", return_value=info) as run:
            result = feed._channel_tab("https://example.test/videos", policy, set())

        self.assertIs(result, info)
        self.assertEqual(run.call_args.args[0][1:3], ["--playlist-end", "50"])

    def test_absent_streams_tab_is_a_complete_empty_section(self) -> None:
        """Treat a channel without streams as valid instead of a partial failure.

        Example: yt-dlp's normal missing-tab result still permits safe pruning.
        """

        feed = library_feed.YtDlpFeed(Path("python"))
        policy = library_feed.ChannelScanPolicy(50, 500, None)
        error = RuntimeError("This channel does not have a streams tab")
        with mock.patch.object(feed, "_json", side_effect=error):
            info = feed._channel_tab(
                "https://youtube.com/@example/streams",
                policy,
                set(),
            )

        self.assertEqual(info, {"entries": []})

    def test_channel_cutoff_filters_dated_entries_but_keeps_unknown_dates(self) -> None:
        """Apply a publication cutoff without guessing when metadata is absent.

        Example: a pre-April row is removed while an undated row awaits hydration.
        """

        feed = library_feed.YtDlpFeed(Path("python"))
        info = {
            "channel": "Example",
            "channel_id": "UC-example",
            "entries": [
                {"id": "aaaaaaaaaaa", "timestamp": 100},
                {"id": "bbbbbbbbbbb", "timestamp": 300},
                {"id": "ccccccccccc"},
            ],
        }
        policy = library_feed.ChannelScanPolicy(50, 500, 200)
        with (
            mock.patch.object(feed, "_channel_tab", return_value=info),
            mock.patch.object(feed, "_rss_dates", return_value={}),
        ):
            snapshot = feed.channel("@example", policy)

        ids = [meta.identity.video_id for meta in snapshot.videos]
        self.assertEqual(ids, ["bbbbbbbbbbb", "ccccccccccc"])
        self.assertTrue(snapshot.complete)


if __name__ == "__main__":
    unittest.main()
