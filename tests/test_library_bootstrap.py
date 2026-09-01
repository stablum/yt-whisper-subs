"""Tests for managed GUI relaunch and terminal interruption behavior.

Example: `python -m unittest tests.test_library_bootstrap`.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from yt_whisper_subs import library_bootstrap


class LibraryBootstrapTests(unittest.TestCase):
    """Keep the outer terminal launcher attached but promptly interruptible.

    Example: `LibraryBootstrapTests("test_ctrl_c_returns_standard_exit_code")`.
    """

    @mock.patch("yt_whisper_subs.library_bootstrap.proc.configure_stdio")
    @mock.patch("yt_whisper_subs.library_bootstrap.proc.ensure_library_deps")
    @mock.patch("yt_whisper_subs.library_bootstrap.proc.run")
    def test_relaunch_uses_interruptible_process_runner(
        self,
        run: mock.Mock,
        _ensure: mock.Mock,
        _stdio: mock.Mock,
    ) -> None:
        """Wait through the polling runner instead of Windows' blocking wait.

        Example: an ordinary child exit is returned unchanged to WezTerm.
        """

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            run.return_value = subprocess.CompletedProcess([], 7)
            with self._launcher_context(root, paths):
                result = library_bootstrap.main()

        self.assertEqual(result, 7)
        cmd = run.call_args.args[0]
        self.assertEqual(Path(cmd[0]), paths["python_gui"])
        self.assertEqual(cmd[1:3], ["-m", "yt_whisper_subs.library_app"])
        self.assertEqual(run.call_args.kwargs, {"check": False, "silence_seconds": None})

    @mock.patch("yt_whisper_subs.library_bootstrap.proc.configure_stdio")
    @mock.patch("yt_whisper_subs.library_bootstrap.proc.ensure_library_deps")
    @mock.patch("yt_whisper_subs.library_bootstrap.proc.run", side_effect=KeyboardInterrupt)
    def test_ctrl_c_returns_standard_exit_code(
        self,
        _run: mock.Mock,
        _ensure: mock.Mock,
        _stdio: mock.Mock,
    ) -> None:
        """Convert an already-cleaned interrupt into conventional exit code 130.

        Example: Ctrl+C kills pythonw.exe and restores the shell prompt.
        """

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            with self._launcher_context(root, paths):
                result = library_bootstrap.main()

        self.assertEqual(result, 130)

    @staticmethod
    def _paths(root: Path) -> dict[str, Path]:
        """Build isolated managed executable paths for bootstrap tests.

        Example: `_paths(tmp)["python_gui"]` names a fake pythonw.exe.
        """

        managed = root / "managed"
        managed.mkdir()
        paths = {
            "python": managed / "python.exe",
            "python_gui": managed / "pythonw.exe",
        }
        paths["python_gui"].touch()
        return paths

    @staticmethod
    @contextmanager
    def _launcher_context(root: Path, paths: dict[str, Path]) -> Iterator[None]:
        """Patch one non-managed invocation without touching the real runtime.

        Example: `with _launcher_context(tmp, paths): bootstrap.main()`.
        """

        with mock.patch(
            "yt_whisper_subs.library_bootstrap.proc.venv_paths",
            return_value=paths,
        ), mock.patch.object(sys, "executable", str(root / "outside" / "python.exe")):
            yield
