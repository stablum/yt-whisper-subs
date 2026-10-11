"""Verify real yt-dlp selection and durable native resolution preferences.

Example: `python -m unittest tests.test_download_prefs` makes no network requests.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yt_dlp

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets  # noqa: E402

from yt_whisper_subs import cli  # noqa: E402
from yt_whisper_subs import download_prefs  # noqa: E402
from yt_whisper_subs import library_gui  # noqa: E402
from yt_whisper_subs import library_service  # noqa: E402
from yt_whisper_subs import library_types as types  # noqa: E402
from yt_whisper_subs import library_widgets  # noqa: E402
from yt_whisper_subs import youtube  # noqa: E402
from tests import test_library as fixtures  # noqa: E402


class ResolutionTests(unittest.TestCase):
    """Exercise the installed downloader's parser rather than duplicate its rules.

    Example: a 1080p combined format still beats separate 4K video/audio.
    """

    def _select(
        self, heights: tuple[int | None, ...], flags: tuple[str, ...] = (),
        *, combined: bool = False, mode: youtube.DownloadMode = youtube.DownloadMode.DEFAULT,
    ) -> dict:
        """Feed synthetic metadata through the actual CLI command and yt-dlp.

        Example: `_select((720, 1080, 2160))` returns a 1080p result offline.
        """

        with mock.patch.object(sys, "argv", ["test", "https://youtu.be/aaaaaaaaaaa", *flags]):
            args = cli.parse_args()
        cmd = youtube.download_command(
            args.url, Path("videos"), Path("metadata"), {"python": Path("python")}, args, mode=mode,
        )
        options = yt_dlp.parse_options(["--ignore-config", *map(str, cmd[3:])]).ydl_opts
        options.update(quiet=True, no_warnings=True)
        formats = [
            {
                "format_id": f"video-{height}", "height": height,
                "width": height * 16 // 9 if height else None,
                "url": f"https://example.invalid/{height}.mp4", "ext": "mp4",
                "vcodec": "avc1", "acodec": "mp4a" if combined else "none",
                # A higher-resolution HLS format must not outrank the exact preference.
                "protocol": "m3u8_native" if height and height > 1080 else "https",
            }
            for height in heights
        ]
        if not combined:
            formats.append({
                "format_id": "audio", "url": "https://example.invalid/audio.m4a",
                "ext": "m4a", "vcodec": "none", "acodec": "mp4a", "abr": 128,
            })
        info = {"id": "test", "title": "Offline format test", "extractor": "test", "formats": formats}
        with yt_dlp.YoutubeDL(options) as downloader:
            return downloader.process_ie_result(info, download=False)

    def test_default_prefers_1080_in_all_transports(self) -> None:
        """Choose full HD ahead of 1440p/4K on every bounded retry.

        Example: HLS sorting still honors the exact-height first choice.
        """

        for mode in youtube.DownloadMode:
            for combined in (False, True):
                with self.subTest(mode=mode, combined=combined):
                    result = self._select((720, 1080, 1440, 2160), combined=combined, mode=mode)
                    self.assertEqual(result["height"], 1080)
                    self.assertNotEqual(result["acodec"], "none")

    def test_missing_preference_accepts_other_resolutions(self) -> None:
        """Retain best-available fallback above, below, or without known height.

        Example: only 720p and 1440p available permits 1440p.
        """

        for heights, expected in (((720, 1440), 1440), ((720,), 720), ((None,), None)):
            with self.subTest(heights=heights):
                self.assertEqual(self._select(heights).get("height"), expected)

    def test_resolution_preference_and_ceiling_are_configurable(self) -> None:
        """Honor another target and prevent exceeding a disabled fallback.

        Example: a 720p ceiling never selects an available 4K stream.
        """

        cases = (
            (("--video-resolution", "720"), (720, 1080, 2160), 720),
            (("--no-allow-higher-resolution",), (720, 1440), 720),
            (("--video-resolution", "0"), (720, 1080, 2160), 2160),
            (("--no-allow-higher-resolution", "--video-format", "bv*+ba/b"), (1080, 2160), 2160),
        )
        for flags, heights, expected in cases:
            with self.subTest(flags=flags):
                self.assertEqual(self._select(heights, flags)["height"], expected)
        with self.assertRaises(yt_dlp.utils.ExtractorError):
            self._select((1440,), ("--no-allow-higher-resolution",))


class NativePrefsTests(unittest.TestCase):
    """Follow native edits through persistence, reload, and the pipeline boundary.

    Example: saved settings survive a new LibraryService instance.
    """

    @classmethod
    def setUpClass(cls) -> None:
        """Provide Qt's single application for native settings controls.

        Example: tests run with the offscreen platform.
        """

        cls._app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_dialog_edits_resolution_and_fallback(self) -> None:
        """Read real controls and disable fallback for unrestricted quality.

        Example: selecting Best available makes the fallback checkbox irrelevant.
        """

        values = library_widgets.SettingsValues(4, "", True, False, 50, "")
        dialog = library_widgets.SettingsDialog(values)
        self.addCleanup(dialog.close)
        resolution = dialog.findChild(QtWidgets.QComboBox, "videoResolution")
        higher = dialog.findChild(QtWidgets.QCheckBox, "allowHigherResolution")
        self.assertEqual(dialog.values().download, download_prefs.VideoPrefs(1080, True))
        resolution.setCurrentIndex(resolution.findData(720))
        higher.setChecked(False)
        self.assertEqual(dialog.values().download, download_prefs.VideoPrefs(720, False))
        resolution.setCurrentIndex(resolution.findData(0))
        self.assertFalse(higher.isEnabled())
        resolution.setCurrentIndex(resolution.findData(1080))
        self.assertTrue(higher.isEnabled())
        self.assertFalse(higher.isChecked())

    @mock.patch("yt_whisper_subs.library_window_actions.windows_startup.set_enabled")
    @mock.patch("yt_whisper_subs.library_window_actions.windows_startup.is_enabled", return_value=False)
    @mock.patch("yt_whisper_subs.library_window_actions.library_widgets.SettingsDialog")
    def test_saved_settings_reach_reloaded_downloads(
        self, dialog_cls: mock.Mock, _is_enabled: mock.Mock, _set_enabled: mock.Mock,
    ) -> None:
        """Save via the window action and inspect the next real pipeline command.

        Example: a reopened library sends a durable 720p ceiling to the CLI.
        """

        values = library_widgets.SettingsValues(
            4, "", True, False, 50, "", download_prefs.VideoPrefs(720, False),
        )
        dialog_cls.return_value.exec.return_value = QtWidgets.QDialog.DialogCode.Accepted
        dialog_cls.return_value.values.return_value = values
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = {"python": Path(sys.executable)}
            service = library_service.LibraryService(root, paths)
            window = mock.Mock()
            window._service = service
            library_gui.LibraryWindow._edit_settings(window)
            self.assertEqual(service.video_prefs(), values.download)
            restarted = library_service.LibraryService(root, paths)
            self.assertEqual(restarted.video_prefs(), values.download)
            # Inspect the real boundary without launching Whisper or downloading media.
            for client in (service, restarted):
                with mock.patch.object(client._downloader, "_run") as run:
                    meta = fixtures.make_meta("aaaaaaaaaaa", "Offline video")
                    record = types.VideoRecord(meta, None, 0, None, None, None)
                    client._downloader.download(record, mock.Mock())
                    cmd = run.call_args.args[1]
                self.assertEqual(cmd[cmd.index("--video-resolution") + 1], "720")
                self.assertIn("--no-allow-higher-resolution", cmd)
