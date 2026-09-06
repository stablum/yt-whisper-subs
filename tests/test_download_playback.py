"""Tests for metadata-preserving downloads and shared playback preferences.

Example: `python -m unittest tests.test_download_playback`.
"""

from __future__ import annotations

import argparse
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from yt_whisper_subs import cfg
from yt_whisper_subs import media
from yt_whisper_subs import mpv_ipc
from yt_whisper_subs import playback
from yt_whisper_subs import playback_progress as progress
from yt_whisper_subs import proc
from yt_whisper_subs import youtube


class DownloadCommandTests(unittest.TestCase):
    """Ensure every new CLI download keeps durable YouTube metadata.

    Example: `DownloadCommandTests("test_command_writes_and_embeds_metadata")`.
    """

    @mock.patch("yt_whisper_subs.youtube.shutil.which")
    def test_command_writes_and_embeds_metadata(self, which: mock.Mock) -> None:
        """Write a sidecar and embed standard tags/info JSON into the container.

        Example: the downloader command contains all metadata flags.
        """

        which.side_effect = lambda name: "node.exe" if name == "node" else None
        args = argparse.Namespace(
            download_progress_delta=1.0,
            video_format="bv*+ba/b",
            merge_output_format="mkv",
            force=False,
            cookies_from_browser=None,
        )
        cmd = youtube.download_command(
            "https://www.youtube.com/watch?v=aaaaaaaaaaa",
            Path("videos"),
            Path("metadata"),
            {"python": Path("python")},
            args,
        )
        text = [str(part) for part in cmd]
        self.assertIn("--write-info-json", text)
        self.assertIn("--embed-metadata", text)
        self.assertIn("--embed-info-json", text)
        self.assertIn(str(Path("infojson:metadata") / "%(id)s.%(ext)s"), text)
        runtime_idx = text.index("--js-runtimes")
        self.assertEqual(text[runtime_idx + 1], "node")

    @mock.patch("yt_whisper_subs.youtube.shutil.which", return_value=None)
    def test_command_tolerates_missing_js_runtime(self, _which: mock.Mock) -> None:
        """Let yt-dlp explain missing runtimes when neither supported tool exists.

        Example: a machine without Deno or Node can still attempt extraction.
        """

        self.assertEqual(youtube.yt_dlp_js_runtime_args(), [])


class PlaybackPrefsTests(unittest.TestCase):
    """Keep GUI and CLI playback on the same defaults and sidecar order.

    Example: `PlaybackPrefsTests("test_defaults_include_user_font_scale")`.
    """

    def test_defaults_include_user_font_scale(self) -> None:
        """Derive the primary font size from the user's changed scale of 0.45.

        Example: the native GUI uses the same value as CLI defaults.
        """

        prefs = playback.PlaybackPrefs.defaults()
        expected = cfg.DEFAULT_DUAL_SUB_FONT_SIZE * cfg.DEFAULT_DUAL_SUB_PRIMARY_FONT_SCALE
        self.assertEqual(prefs.primary.font_size, expected)
        self.assertTrue(prefs.dual_subs)

    def test_sidecars_are_english_first(self) -> None:
        """Match the current CLI's English-primary, Dutch-secondary mpv order.

        Example: GUI double-click discovers both neighboring SRT files.
        """

        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "aaaaaaaaaaa.mkv"
            video.write_bytes(b"video")
            primary = video.with_suffix(".srt")
            english = video.with_name("aaaaaaaaaaa.en.srt")
            primary.write_text("primary", encoding="utf-8")
            english.write_text("english", encoding="utf-8")
            self.assertEqual(playback.sidecar_subtitles(video), [english, primary])

    @mock.patch("yt_whisper_subs.playback.proc.run")
    def test_mpv_uses_visible_application_policy(self, run: mock.Mock) -> None:
        """Keep mpv visible without weakening hidden background tools.

        Example: GUI double-click produces a taskbar and Alt+Tab window.
        """

        playback.play_video(Path("video.mkv"), [], playback.PlaybackPrefs.defaults())

        self.assertEqual(run.call_args.kwargs["window"], proc.ChildWindow.VISIBLE)
        self.assertFalse(
            any(str(arg).startswith("--input-ipc-server=") for arg in run.call_args.args[0])
        )

    @mock.patch("yt_whisper_subs.playback.proc.run")
    @mock.patch("yt_whisper_subs.playback.mpv_ipc.MpvMonitor")
    def test_observer_adds_ephemeral_ipc_at_launch(
        self,
        monitor: mock.Mock,
        run: mock.Mock,
    ) -> None:
        """Add a unique IPC option without changing or bypassing mpv config.

        Example: library playback receives events while CLI playback stays plain.
        """

        monitor.return_value.mpv_option = "--input-ipc-server=test-pipe"
        observer = progress.Observer("aaaaaaaaaaa", mock.Mock())

        playback.play_video(
            Path("video.mkv"),
            [],
            playback.PlaybackPrefs.defaults(),
            playback.PlaybackSession(observer=observer),
        )

        cmd = [str(arg) for arg in run.call_args.args[0]]
        self.assertIn("--input-ipc-server=test-pipe", cmd)
        self.assertNotIn("--no-config", cmd)
        monitor.return_value.__enter__.assert_called_once()


class MediaDurationTests(unittest.TestCase):
    """Keep chapter density tied to the actual video container duration.

    Example: `MediaDurationTests("test_ffprobe_duration_is_milliseconds")`.
    """

    @mock.patch("yt_whisper_subs.media.proc.require_command")
    @mock.patch("yt_whisper_subs.media.proc.run")
    def test_ffprobe_duration_is_milliseconds(self, run: mock.Mock, require: mock.Mock) -> None:
        """Convert ffprobe's fractional seconds without parsing human output.

        Example: `12.345` seconds becomes `12345` milliseconds.
        """

        run.return_value.stdout = "12.345\n"

        self.assertEqual(media.probe_duration_ms(Path("video.mkv")), 12_345)
        require.assert_called_once_with("ffprobe")
        self.assertIn("format=duration", run.call_args.args[0])


class PlaybackChapterTests(unittest.TestCase):
    """Verify launch-only mpv chapter navigation options.

    Example: `PlaybackChapterTests("test_launch_adds_chapters_and_exact_start_without_config_changes")`.
    """

    @mock.patch("yt_whisper_subs.playback.proc.run")
    def test_launch_adds_chapters_and_exact_start_without_config_changes(self, run: mock.Mock) -> None:
        """Load generated navigation only for this mpv process and seek locally.

        Example: a GUI chapter double-click starts at 12:30.
        """

        with tempfile.TemporaryDirectory() as tmp:
            chapter_path = Path(tmp) / "video.chapters.ffmetadata"
            chapter_path.write_text(";FFMETADATA1\n", encoding="utf-8")
            session = playback.PlaybackSession(
                chapter_path=chapter_path,
                start_seconds=750.0,
            )

            playback.play_video(
                Path("video.mkv"),
                [],
                playback.PlaybackPrefs.defaults(),
                session,
            )

        cmd = [str(arg) for arg in run.call_args.args[0]]
        self.assertIn(f"--chapters-file={chapter_path}", cmd)
        self.assertIn("--start=750", cmd)
        self.assertNotIn("--no-config", cmd)


class MpvEventTrackerTests(unittest.TestCase):
    """Convert mpv property and terminal events into trustworthy progress.

    Example: `MpvEventTrackerTests("test_eof_alone_reaches_100_percent")`.
    """

    def test_eof_alone_reaches_100_percent(self) -> None:
        """Pace ordinary updates and reserve completion for the EOF reason.

        Example: a position equal to duration remains 99 percent before EOF.
        """

        updates: list[progress.Update] = []
        tracker = mpv_ipc.MpvEventTracker(
            progress.Observer("aaaaaaaaaaa", updates.append),
            emit_interval=5,
        )
        tracker.ingest({"event": "property-change", "name": "duration", "data": 100.0}, now=0)
        tracker.ingest({"event": "property-change", "name": "time-pos", "data": 10.0}, now=0)
        tracker.ingest({"event": "property-change", "name": "time-pos", "data": 40.0}, now=1)
        tracker.ingest({"event": "property-change", "name": "time-pos", "data": 100.0}, now=5)

        self.assertEqual(len(updates), 2)
        self.assertEqual(progress.fraction(updates[-1]), 0.99)

        tracker.ingest({"event": "end-file", "reason": "eof"}, now=6)

        self.assertTrue(updates[-1].completed)
        self.assertEqual(progress.fraction(updates[-1]), 1.0)
        self.assertFalse(tracker.finish())

    def test_quit_flushes_progress_without_completion(self) -> None:
        """Distinguish a normal window close from reaching the end of the file.

        Example: `end-file: quit` persists the position below 100 percent.
        """

        updates: list[progress.Update] = []
        tracker = mpv_ipc.MpvEventTracker(
            progress.Observer("aaaaaaaaaaa", updates.append),
            emit_interval=60,
        )
        tracker.ingest({"event": "property-change", "name": "duration", "data": 200.0}, now=0)
        tracker.ingest({"event": "property-change", "name": "time-pos", "data": 50.0}, now=0)
        tracker.ingest({"event": "end-file", "reason": "quit"}, now=1)

        self.assertFalse(updates[-1].completed)
        self.assertEqual(progress.fraction(updates[-1]), 0.25)

    def test_progress_protocol_round_trip(self) -> None:
        """Carry one playback update through the existing string signal safely.

        Example: a worker report becomes a typed GUI model update.
        """

        update = progress.make("aaaaaaaaaaa", 30, 120)

        self.assertEqual(progress.parse(progress.encode(update)), update)
        self.assertIsNone(progress.parse("ordinary trace output"))


if __name__ == "__main__":
    unittest.main()
