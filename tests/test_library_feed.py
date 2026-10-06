"""Regress channel identity loss when public upload tabs are absent.

Example: `python -m unittest tests.test_library_feed` checks empty subscriptions.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import test_library as fixtures
from yt_whisper_subs import library_feed
from yt_whisper_subs import library_service
from yt_whisper_subs import library_types as types


class ChannelIdentityTests(unittest.TestCase):
    """Exercise actual tab discovery through persisted subscription state.

    Example: absent tabs must not produce a URL-titled successful baseline.
    """

    def test_empty_channel_resolves_and_persists_identity_without_home_entries(self) -> None:
        """Save an empty channel's header while excluding its home-page content.

        Example: Tom Jessen has no Videos or Streams tab but still has a title.
        """

        url = "https://www.youtube.com/channel/UC-example/videos"
        header = {
            "channel_id": "UC-example",
            "channel": "Example channel",
            "entries": [{"id": "aaaaaaaaaaa", "title": "Home-page content"}],
        }
        feed = library_feed.YtDlpFeed(Path("python"))
        responses = [
            RuntimeError("This channel does not have a videos tab"),
            RuntimeError("This channel does not have a streams tab"),
            header,
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            downloader = fixtures.FakeDownloader(root)
            service = library_service.LibraryService(
                root, {"python": Path("python")}, feed=feed, downloader=downloader
            )
            channel = service.db.add_channel(url, url, True)
            with (
                mock.patch.object(feed, "_json", side_effect=responses) as lookup,
                mock.patch.object(feed, "_atom_metadata") as atom,
            ):
                service.initialize_channel(channel.channel_id)
            saved = service.db.channel(channel.channel_id)
            self.assertEqual(saved.title, "Example channel")
            self.assertEqual(saved.youtube_id, "UC-example")
            self.assertIsNotNone(saved.baseline_at)
            self.assertIsNone(saved.last_error)
            self.assertEqual(service.db.videos(), [])
            self.assertEqual(downloader.calls, [])
            self.assertEqual(
                lookup.call_args.args[0],
                ["--flat-playlist", "--playlist-items", "0", "--dump-single-json", url.removesuffix("/videos")],
            )
            atom.assert_not_called()

    def test_streams_only_channel_uses_stream_identity_and_retains_videos(self) -> None:
        """Resolve a stream-only channel without an extra root lookup.

        Example: a missing Videos tab cannot hide the Streams title or replay.
        """

        feed = library_feed.YtDlpFeed(Path("python"))
        streams = {
            "channel_id": "UC-example",
            "channel": "Streams channel",
            "entries": [{"id": "aaaaaaaaaaa", "title": "Broadcast"}],
        }
        responses = [RuntimeError("This channel does not have a videos tab"), streams]
        policy = library_feed.ChannelScanPolicy(50, 500, None)
        with (
            mock.patch.object(feed, "_json", side_effect=responses) as lookup,
            mock.patch.object(feed, "_atom_metadata", return_value={}),
        ):
            snapshot = feed.channel("@example", policy)
        self.assertEqual(lookup.call_count, 2)
        self.assertEqual(snapshot.title, "Streams channel")
        self.assertEqual(snapshot.youtube_id, "UC-example")
        self.assertTrue(snapshot.complete)
        self.assertEqual(snapshot.videos[0].identity.video_id, "aaaaaaaaaaa")
        self.assertEqual(snapshot.videos[0].details.live_status, types.LIVE_UNKNOWN)

    def test_root_failure_keeps_existing_history_and_baseline_unset(self) -> None:
        """Preserve catalog history when the independent identity lookup fails.

        Example: a network failure must not establish a destructive empty snapshot.
        """

        feed = library_feed.YtDlpFeed(Path("python"))
        responses = [
            RuntimeError("This channel does not have a videos tab"),
            RuntimeError("This channel does not have a streams tab"),
            RuntimeError("connection refused"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            service = library_service.LibraryService(Path(tmp), {"python": Path("python")}, feed=feed)
            channel = service.track_channel("@example", False)
            service.db.upsert_video(fixtures.make_meta("aaaaaaaaaaa", "Known video"), channel.channel_id)
            with mock.patch.object(feed, "_json", side_effect=responses):
                with self.assertRaisesRegex(RuntimeError, "connection refused"):
                    service.initialize_channel(channel.channel_id)
            saved = service.db.channel(channel.channel_id)
            self.assertIsNone(saved.baseline_at)
            self.assertIn("connection refused", saved.last_error)
            self.assertIsNotNone(service.db.video("aaaaaaaaaaa"))

    def test_incomplete_root_identity_is_rejected(self) -> None:
        """Reject malformed headers instead of fabricating a successful title.

        Example: an empty extractor document cannot become a URL-titled channel.
        """

        feed = library_feed.YtDlpFeed(Path("python"))
        policy = library_feed.ChannelScanPolicy(50, 500, None)
        for header in ({}, {"channel_id": "UC-example"}, {"channel": "Example"}):
            with (
                self.subTest(header=header),
                mock.patch.object(feed, "_channel_tab", return_value={"entries": []}),
                mock.patch.object(feed, "_json", return_value=header),
            ):
                with self.assertRaisesRegex(RuntimeError, "without an ID or title"):
                    feed.channel("@example", policy)

    def test_genuine_videos_failure_does_not_become_an_empty_channel(self) -> None:
        """Keep primary-tab extraction failures distinct from authoritative absence.

        Example: a removed channel or network refusal remains a visible error.
        """

        feed = library_feed.YtDlpFeed(Path("python"))
        policy = library_feed.ChannelScanPolicy(50, 500, None)
        with mock.patch.object(feed, "_json", side_effect=RuntimeError("channel unavailable")) as lookup:
            with self.assertRaisesRegex(RuntimeError, "channel unavailable"):
                feed.channel("@example", policy)
        self.assertEqual(lookup.call_count, 1)
