"""Stdlib-only bootstrap that installs and enters the managed Qt runtime.

Example: `library_bootstrap.main()` is called by the `.pyw` launcher.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from yt_whisper_subs import cfg
from yt_whisper_subs import proc


def main() -> int:
    """Relaunch in the managed environment or enter Qt when already there.

    Example: `raise SystemExit(main())` from `yt_whisper_library.pyw`.
    """

    proc.configure_stdio()
    paths = proc.venv_paths()
    current = Path(sys.executable).resolve()
    managed_executables = {paths["python"].resolve(), paths["python_gui"].resolve()}
    if current in managed_executables and proc.managed_module_available(paths["python"], "PySide6"):
        from yt_whisper_subs import library_app

        return library_app.main(sys.argv[1:])

    proc.ensure_library_deps(paths, cfg.DEFAULT_PYTHON_VERSION)
    gui_python = paths["python_gui"] if paths["python_gui"].exists() else paths["python"]
    cmd = [str(gui_python), "-m", "yt_whisper_subs.library_app", *sys.argv[1:]]
    return subprocess.run(cmd, **proc.child_process_kwargs(), check=False).returncode
