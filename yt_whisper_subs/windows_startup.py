"""Manage the current user's Windows login launch without administrator rights.

Example: `windows_startup.set_enabled(True, output_root)` registers the GUI.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from yt_whisper_subs import cfg
from yt_whisper_subs import proc

try:
    import winreg as _winreg
except ImportError:  # pragma: no cover - exercised only on non-Windows hosts
    _winreg = None


_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = "yt-whisper-subs YouTube Library"


def supported() -> bool:
    """Report whether per-user Windows startup registration is available.

    Example: the Settings dialog disables its checkbox on non-Windows hosts.
    """

    return _winreg is not None


def launch_command(out_dir: Path, paths: dict[str, Path] | None = None) -> str:
    """Build the quoted console-free command stored in the Windows Run key.

    Example: `launch_command(Path("D:/Videos"))` includes `--start-hidden`.
    """

    managed = paths or proc.venv_paths()
    argv = [
        str(managed["python_gui"].resolve()),
        str((cfg.PROJECT_DIR / "yt_whisper_library.pyw").resolve()),
        "--out-dir",
        str(out_dir.resolve()),
        "--start-hidden",
    ]
    return subprocess.list2cmdline(argv)


def registered_command() -> str | None:
    """Read this app's current-user login command, if one exists.

    Example: an unconfigured PC returns `None`.
    """

    registry = _require_registry()
    try:
        with registry.OpenKey(registry.HKEY_CURRENT_USER, _RUN_KEY) as key:
            value, _value_type = registry.QueryValueEx(key, _VALUE_NAME)
    except FileNotFoundError:
        return None
    return str(value)


def is_enabled(out_dir: Path) -> bool:
    """Confirm that Windows holds exactly the command this installation needs.

    Example: moving the project makes an old registration read as disabled.
    """

    if not supported():
        return False
    return registered_command() == launch_command(out_dir)


def set_enabled(enabled: bool, out_dir: Path) -> None:
    """Create or remove only this app's HKCU Run value.

    Example: `set_enabled(False, root)` leaves every other startup app intact.
    """

    registry = _require_registry()
    if enabled:
        with registry.CreateKey(registry.HKEY_CURRENT_USER, _RUN_KEY) as key:
            registry.SetValueEx(
                key,
                _VALUE_NAME,
                0,
                registry.REG_SZ,
                launch_command(out_dir),
            )
        return
    try:
        with registry.OpenKey(
            registry.HKEY_CURRENT_USER,
            _RUN_KEY,
            0,
            registry.KEY_SET_VALUE,
        ) as key:
            registry.DeleteValue(key, _VALUE_NAME)
    except FileNotFoundError:
        return


def _require_registry() -> Any:
    """Return the platform registry module or reject unsupported hosts clearly.

    Example: accidental registration on Linux raises `RuntimeError`.
    """

    if _winreg is None:
        raise RuntimeError("Start with Windows is available only on Windows.")
    return _winreg
