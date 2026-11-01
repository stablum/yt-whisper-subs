"""Follow seek-step edits through CLI parsing, native settings, persistence, and playback.

Example: `python -m unittest tests.test_playback_settings` needs no network or player.
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets  # noqa: E402

from yt_whisper_subs import cfg  # noqa: E402
from yt_whisper_subs import cli  # noqa: E402
from yt_whisper_subs import library_service  # noqa: E402
from yt_whisper_subs import library_widgets  # noqa: E402
from yt_whisper_subs import library_window_actions  # noqa: E402
from yt_whisper_subs import playback  # noqa: E402


class PlaybackSettingsTests(unittest.TestCase):
    """Keep seek configuration consistent across entry points; e.g. reopen a saved library."""

    @classmethod
    def setUpClass(cls) -> None:
        """Share Qt's application for real settings controls; e.g. an offscreen spin box."""

        cls._app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self) -> None:
        """Isolate saved settings from the user's catalog; e.g. configure a temporary library."""

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self._root = Path(temp.name)
        self._service = library_service.LibraryService(self._root, {"python": Path(sys.executable)})
        self.addCleanup(self._service.vocabulary.close)

    def test_cli_step_reaches_playback_and_rejects_invalid_values(self) -> None:
        """Validate fractional steps and endpoints before launching; e.g. refuse NaN or zero."""

        for seconds in (0.05, 0.75, 2.75, 3600):
            with self.subTest(seconds=seconds):
                argv = ["yt_whisper_subs.py", "--video-file", "sample.mkv", "--mpv-seek-seconds", str(seconds)]
                with mock.patch.object(sys, "argv", argv):
                    prefs = playback.PlaybackPrefs.from_args(cli.parse_args())
                self.assertEqual(prefs.seek_seconds, seconds)
        for value in ("0", "-1", "0.01", "3601", "nan", "inf", "-inf", "invalid"):
            with self.subTest(value=value):
                argv = ["yt_whisper_subs.py", "--video-file", "sample.mkv", f"--mpv-seek-seconds={value}"]
                with mock.patch.object(sys, "argv", argv), mock.patch.object(sys, "stderr", io.StringIO()):
                    with self.assertRaises(SystemExit) as stopped:
                        cli.parse_args()
                self.assertEqual(stopped.exception.code, 2)

    def test_edited_step_survives_restart_and_reaches_both_library_playback_paths(self) -> None:
        """Save a real control edit and inspect later launches; e.g. normal and vocabulary playback."""

        values = library_widgets.SettingsValues(4, "", True, False, 50, "")
        dialog = library_widgets.SettingsDialog(values)
        self.addCleanup(dialog.close)
        control = dialog.findChild(QtWidgets.QDoubleSpinBox, "mpvSeekSeconds")
        self.assertEqual(control.value(), cfg.DEFAULT_MPV_SEEK_SECONDS)
        control.setValue(2.75)
        values = dialog.values()
        window = mock.Mock(_service=self._service)
        with (
            mock.patch("yt_whisper_subs.library_window_actions.windows_startup.is_enabled", return_value=False),
            mock.patch("yt_whisper_subs.library_window_actions.windows_startup.set_enabled"),
            mock.patch("yt_whisper_subs.library_window_actions.library_widgets.SettingsDialog") as dialog_cls,
        ):
            dialog_cls.return_value.exec.return_value = QtWidgets.QDialog.DialogCode.Accepted
            dialog_cls.return_value.values.return_value = values
            library_window_actions.WindowActionsMixin._edit_settings(window)
        reopened = library_service.LibraryService(self._root, {"python": Path(sys.executable)})
        self.addCleanup(reopened.vocabulary.close)
        self.assertEqual(reopened.playback_prefs().seek_seconds, 2.75)
        restored = library_widgets.SettingsDialog(values._replace(playback=reopened.playback_prefs()))
        self.addCleanup(restored.close)
        self.assertEqual(restored.findChild(QtWidgets.QDoubleSpinBox, "mpvSeekSeconds").value(), 2.75)
        reopened.video_dir.mkdir(exist_ok=True)
        video = reopened.video_dir / "aaaaaaaaaaa.mkv"
        video.write_bytes(b"video")
        reopened.scan_local()
        with (
            mock.patch("yt_whisper_subs.library_service.proc.require_command"),
            mock.patch("yt_whisper_subs.playback.proc.run") as run,
        ):
            reopened.play("aaaaaaaaaaa")
            self.assertIn("--script-opts-append=ytw_seek-seconds=2.75", run.call_args.args[0])
            # A source outside the catalog takes the independent vocabulary launch path.
            reopened.play_occurrence({
                "cue_start_ms": 1000, "video_id": None, "source_key": "file:sample",
                "title": "Sample", "url": "", "path": str(video), "language": "nl",
            })
            self.assertEqual(run.call_count, 2)
            self.assertIn("--script-opts-append=ytw_seek-seconds=2.75", run.call_args.args[0])

    def test_invalid_saved_step_uses_default(self) -> None:
        """Recover from corrupt settings before constructing mpv options; e.g. a saved infinity."""

        for value in ("bad", "nan", "inf", "0", "-1", "0.01", "3601"):
            with self.subTest(value=value):
                self._service.db.set_setting("mpv_seek_seconds", value)
                self.assertEqual(self._service.playback_prefs().seek_seconds, cfg.DEFAULT_MPV_SEEK_SECONDS)
