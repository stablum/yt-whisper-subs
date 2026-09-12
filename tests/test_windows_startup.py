"""Tests for exact current-user Windows login registration.

Example: `python -m unittest tests.test_windows_startup`.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Self
from unittest import mock

from yt_whisper_subs import windows_startup


class _Key:
    """Act as the minimal context-managed registry key used by the adapter.

    Example: `with _Key() as key` returns that same key.
    """

    def __enter__(self) -> Self:
        """Return this fake key for a registry context.

        Example: tests receive the key passed to `SetValueEx`.
        """

        return self

    def __exit__(self, *_args: object) -> None:
        """Close the fake context without suppressing errors.

        Example: leaving a `with` block invokes this method.
        """


class WindowsStartupTests(unittest.TestCase):
    """Verify quoting and surgical writes without touching the real registry.

    Example: `WindowsStartupTests("test_enable_writes_one_named_value")`.
    """

    def test_launch_command_is_hidden_and_preserves_spaced_paths(self) -> None:
        """Use pythonw and carry the active output root into a tray launch.

        Example: a root containing spaces remains one command-line argument.
        """

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Video Library"
            paths = {"python_gui": Path(tmp) / "Managed Python" / "pythonw.exe"}
            command = windows_startup.launch_command(root, paths)

        self.assertIn('"' + str(paths["python_gui"].resolve()) + '"', command)
        self.assertIn('"' + str(root.resolve()) + '"', command)
        self.assertIn("yt_whisper_library.pyw", command)
        self.assertTrue(command.endswith("--start-hidden"))

    def test_enable_writes_one_named_value(self) -> None:
        """Register the exact app value below HKCU without broad mutations.

        Example: enabling writes a single `REG_SZ` command.
        """

        registry = mock.Mock()
        registry.HKEY_CURRENT_USER = object()
        registry.REG_SZ = 1
        registry.CreateKey.return_value = _Key()
        with mock.patch.object(windows_startup, "_winreg", registry):
            windows_startup.set_enabled(True, Path("D:/Videos"))

        registry.CreateKey.assert_called_once()
        registry.SetValueEx.assert_called_once()
        _key, name, reserved, value_type, command = registry.SetValueEx.call_args.args
        self.assertEqual(name, "yt-whisper-subs YouTube Library")
        self.assertEqual(reserved, 0)
        self.assertEqual(value_type, registry.REG_SZ)
        self.assertIn("--start-hidden", command)

    def test_disable_deletes_only_the_app_value(self) -> None:
        """Remove the app value while leaving the shared Run key intact.

        Example: disabling calls `DeleteValue`, never `DeleteKey`.
        """

        registry = mock.Mock()
        registry.HKEY_CURRENT_USER = object()
        registry.KEY_SET_VALUE = 2
        registry.OpenKey.return_value = _Key()
        with mock.patch.object(windows_startup, "_winreg", registry):
            windows_startup.set_enabled(False, Path("D:/Videos"))

        registry.DeleteValue.assert_called_once_with(
            mock.ANY,
            "yt-whisper-subs YouTube Library",
        )
        self.assertFalse(hasattr(registry, "DeleteKey") and registry.DeleteKey.called)

    def test_enabled_requires_the_current_exact_command(self) -> None:
        """Treat stale registrations from moved installations as not enabled.

        Example: a different output directory does not tick the GUI checkbox.
        """

        root = Path("D:/Videos")
        expected = windows_startup.launch_command(root)
        with mock.patch.object(windows_startup, "_winreg", mock.Mock()), mock.patch.object(
            windows_startup,
            "registered_command",
            return_value=expected,
        ):
            self.assertTrue(windows_startup.is_enabled(root))
            self.assertFalse(windows_startup.is_enabled(Path("E:/Elsewhere")))


if __name__ == "__main__":
    unittest.main()
