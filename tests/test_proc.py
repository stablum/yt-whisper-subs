"""Tests for shared subprocess environment and Windows presentation policy.

Example: `python -m unittest tests.test_proc`.
"""

from __future__ import annotations

import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from yt_whisper_subs import proc


class ChildProcessOptionsTests(unittest.TestCase):
    """Keep UTF-8 output and window visibility policy single-sourced.

    Example: `ChildProcessOptionsTests("test_hidden_child_process_options")`.
    """

    def test_hidden_child_process_options(self) -> None:
        """Hide Windows child consoles while preserving the UTF-8 environment.

        Example: GUI-launched yt-dlp does not flash a terminal window.
        """

        kwargs = proc.child_process_kwargs()
        env = kwargs["env"]
        self.assertEqual(env["PYTHONUTF8"], "1")
        self.assertEqual(env["PYTHONIOENCODING"], "utf-8")

        if os.name == "nt":
            self.assertEqual(kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)
            startup_info = kwargs["startupinfo"]
            self.assertTrue(startup_info.dwFlags & subprocess.STARTF_USESHOWWINDOW)
            self.assertEqual(startup_info.wShowWindow, subprocess.SW_HIDE)
        else:
            self.assertNotIn("creationflags", kwargs)
            self.assertNotIn("startupinfo", kwargs)

    def test_visible_application_suppresses_only_its_console(self) -> None:
        """Allow a GUI window while preventing a companion console flash.

        Example: mpv remains visible in the taskbar and Alt+Tab switcher.
        """

        kwargs = proc.child_process_kwargs(proc.ChildWindow.VISIBLE)
        if os.name == "nt":
            self.assertEqual(kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)
            self.assertNotIn("startupinfo", kwargs)
        else:
            self.assertNotIn("creationflags", kwargs)
            self.assertNotIn("startupinfo", kwargs)

    def test_isolated_process_options_support_tree_cancellation(self) -> None:
        """Place a GUI pipeline in a process group that can be stopped safely.

        Example: cancelling the parent also terminates Whisper and ffmpeg.
        """

        kwargs = proc.isolated_process_kwargs()
        if os.name == "nt":
            self.assertTrue(kwargs["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP)
        else:
            self.assertTrue(kwargs["start_new_session"])

    def test_output_records_split_terminal_progress(self) -> None:
        """Expose carriage-return progress as individual live trace messages.

        Example: yt-dlp's changing percentage becomes three GUI rows.
        """

        stream = io.StringIO("download 10%\rdownload 50%\rdownload 100%\nDone")
        self.assertEqual(
            list(proc.iter_output_records(stream)),
            ["download 10%", "download 50%", "download 100%", "Done"],
        )

    @mock.patch("yt_whisper_subs.proc._terminate_process")
    @mock.patch("yt_whisper_subs.proc._stream_process_output", side_effect=KeyboardInterrupt)
    @mock.patch("yt_whisper_subs.proc.subprocess.Popen")
    def test_run_terminates_child_on_keyboard_interrupt(
        self,
        popen: mock.Mock,
        _stream: mock.Mock,
        terminate: mock.Mock,
    ) -> None:
        """Terminate a detached child before returning terminal control.

        Example: Ctrl+C at the library bootstrap cannot orphan pythonw.exe.
        """

        with self.assertRaises(KeyboardInterrupt):
            proc.run(["child"])

        terminate.assert_called_once_with(popen.return_value)

    @mock.patch("yt_whisper_subs.proc.managed_module_available", return_value=True)
    @mock.patch("yt_whisper_subs.proc.run")
    def test_recent_yt_dlp_check_skips_update(self, run: mock.Mock, _available: mock.Mock) -> None:
        """Avoid a package-index request on every GUI startup.

        Example: a fresh update stamp suppresses the weekly refresh.
        """

        with tempfile.TemporaryDirectory() as tmp:
            venv_dir = Path(tmp)
            (venv_dir / ".yt-dlp-update-check").touch()
            paths = {"python": venv_dir / "python.exe", "venv_dir": venv_dir}
            proc.refresh_yt_dlp(paths)

        run.assert_not_called()

    @mock.patch("yt_whisper_subs.proc.require_command")
    @mock.patch("yt_whisper_subs.proc.managed_module_available", return_value=False)
    @mock.patch("yt_whisper_subs.proc.run")
    def test_yt_dlp_refresh_installs_default_extras(
        self,
        run: mock.Mock,
        _available: mock.Mock,
        _require: mock.Mock,
    ) -> None:
        """Install the EJS component and record a successful update check.

        Example: a new managed environment receives `yt-dlp[default]`.
        """

        run.return_value = subprocess.CompletedProcess([], 0)
        with tempfile.TemporaryDirectory() as tmp:
            venv_dir = Path(tmp)
            paths = {"python": venv_dir / "python.exe", "venv_dir": venv_dir}
            proc.refresh_yt_dlp(paths)

            self.assertTrue((venv_dir / ".yt-dlp-update-check").exists())

        cmd = run.call_args.args[0]
        self.assertIn("yt-dlp[default]", cmd)


if __name__ == "__main__":
    unittest.main()
