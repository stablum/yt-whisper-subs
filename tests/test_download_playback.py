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
from yt_whisper_subs import playback
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


if __name__ == "__main__":
    unittest.main()
