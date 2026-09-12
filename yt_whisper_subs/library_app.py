"""Native library application entry point and minimal argument parsing.

Example: `python -m yt_whisper_subs.library_app --out-dir D:\\Videos`.
"""

from __future__ import annotations

import argparse
import sys

from PySide6 import QtWidgets

from yt_whisper_subs import library_gui
from yt_whisper_subs import library_service
from yt_whisper_subs import pipeline
from yt_whisper_subs import proc


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse only the output root needed by the library companion.

    Example: `parse_args(["--out-dir", "D:\\Videos"])`.
    """

    parser = argparse.ArgumentParser(description="Open the native yt-whisper-subs YouTube library.")
    parser.add_argument("--out-dir", help="Output root shared with the subtitle CLI.")
    parser.add_argument(
        "--start-hidden",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Create the service and run the native Qt event loop.

    Example: `raise SystemExit(main())` from the Windows launcher.
    """

    proc.configure_stdio()
    args = parse_args(argv)
    out_dir = pipeline.resolve_output_dir(args.out_dir)
    qt_app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])
    qt_app.setApplicationName("yt-whisper-subs YouTube Library")
    qt_app.setOrganizationName("yt-whisper-subs")
    qt_app.setQuitOnLastWindowClosed(False)
    try:
        service = library_service.LibraryService(out_dir, proc.venv_paths())
        service.scan_local()
    except Exception as exc:
        QtWidgets.QMessageBox.critical(None, "Could not open YouTube Library", str(exc))
        return 1
    window = library_gui.LibraryWindow(service)
    start_in_tray = args.start_hidden and QtWidgets.QSystemTrayIcon.isSystemTrayAvailable()
    if not start_in_tray:
        window.show()
    return qt_app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
