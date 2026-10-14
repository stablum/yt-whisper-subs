"""Check native Windows visibility instead of relying only on Qt's state.

Example: `python -m unittest tests.test_library_window_startup` reproduces SW_HIDE.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

from yt_whisper_subs import cfg
from yt_whisper_subs import proc


_PROBE = textwrap.dedent('''\
    import ctypes
    import json
    from ctypes import wintypes
    from PySide6 import QtCore, QtWidgets
    from yt_whisper_subs import library_window_support

    class Window(library_window_support.WindowRuntimeMixin, QtWidgets.QWidget):
        """Exercise production tray restoration; e.g. an initially hidden window."""

    user32 = ctypes.WinDLL("user32")
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    app = QtWidgets.QApplication([])
    app.setQuitOnLastWindowClosed(False)
    window = Window()
    window.setWindowTitle("Library startup regression check")
    window.resize(400, 120)
    observed = []

    def inspect():
        """Compare both visibility states; e.g. Qt can say visible while Windows hides it."""
        observed.append({
            "qt_visible": window.isVisible(),
            "native_visible": bool(user32.IsWindowVisible(int(window.winId()))),
        })

    def restore():
        """Invoke the real tray action; e.g. recover an inherited SW_HIDE startup."""
        inspect()
        window._show_window()

    def finish():
        """Return bounded diagnostics without leaving a window; e.g. test child exit."""
        inspect()
        window.hide()
        print(json.dumps(observed), flush=True)
        app.quit()

    window.show()
    QtCore.QTimer.singleShot(150, restore)
    QtCore.QTimer.singleShot(300, finish)
    app.exec()
''')


@unittest.skipUnless(os.name == "nt", "Windows startup visibility flags")
class WindowStartupTests(unittest.TestCase):
    """Run real native children under the same process policies as the bootstrap.

    Example: both startup and tray recovery must make a native window visible.
    """

    def test_gui_launch_and_tray_restore_use_actual_native_visibility(self) -> None:
        """Recover mismatched Qt state while keeping normal GUI startup visible.

        Example: the old hidden policy reproduces the exact invisible-window bug.
        """

        gui_python = Path(sys.executable).with_name("pythonw.exe")
        if not gui_python.is_file():
            self.skipTest("pythonw.exe is unavailable")
        for policy in proc.ChildWindow:
            with self.subTest(policy=policy):
                kwargs = proc.child_process_kwargs(policy, cwd=cfg.PROJECT_DIR)
                # Use a fresh Windows backend even when the main suite is offscreen.
                kwargs["env"]["QT_QPA_PLATFORM"] = "windows"
                kwargs["stdin"] = subprocess.DEVNULL
                result = subprocess.run(
                    [str(gui_python), "-c", _PROBE],
                    **kwargs,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=10,
                    check=True,
                )
                before, after = json.loads(result.stdout)
                self.assertEqual(result.stderr, "")
                self.assertTrue(before["qt_visible"])
                self.assertEqual(before["native_visible"], policy is proc.ChildWindow.VISIBLE)
                self.assertTrue(after["qt_visible"])
                self.assertTrue(after["native_visible"])


if __name__ == "__main__":
    unittest.main()
