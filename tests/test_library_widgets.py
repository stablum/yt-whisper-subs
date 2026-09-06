"""Tests for the native chapter inspector's state and jump signal.

Example: `python -m unittest tests.test_library_widgets`.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets  # noqa: E402

from yt_whisper_subs import chapters  # noqa: E402
from yt_whisper_subs import library_types as types  # noqa: E402
from yt_whisper_subs import library_widgets  # noqa: E402


class DetailPanelTests(unittest.TestCase):
    """Verify chapter presentation without showing a native window.

    Example: `DetailPanelTests("test_bilingual_chapters_emit_seek_time")`.
    """

    @classmethod
    def setUpClass(cls) -> None:
        """Create the single QApplication required by widget construction.

        Example: handled once by `unittest`.
        """

        cls._app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_bilingual_chapters_emit_seek_time(self) -> None:
        """Show both titles and emit the exact double-click timestamp.

        Example: a 90-second chapter emits `90.0` to the window action.
        """

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "aaaaaaaaaaa.mkv"
            path.write_bytes(b"video")
            record = self._record(path)
            chapter_set = chapters.ChapterSet(
                video_id="aaaaaaaaaaa",
                primary_language="nl",
                model="mock",
                generated_at=1,
                duration_ms=180_000,
                chapters=[
                    chapters.Chapter(0, "Inleiding", "Introduction"),
                    chapters.Chapter(90_000, "Vragen", "Questions"),
                ],
            )
            panel = library_widgets.DetailPanel()
            starts: list[float] = []
            panel.chapter_activated.connect(starts.append)

            panel.set_record(record, chapter_set)
            chapter_list = panel.findChild(QtWidgets.QListWidget, "chapterList")
            generate = panel.findChild(QtWidgets.QPushButton, "chapterGenerate")
            self.assertEqual(chapter_list.count(), 2)
            self.assertIn("Vragen", chapter_list.item(1).text())
            self.assertIn("Questions", chapter_list.item(1).text())
            panel._activate_chapter(chapter_list.item(1))
            self.assertEqual(starts, [90.0])
            self.assertTrue(generate.isEnabled())
            panel.set_busy(True)
            self.assertFalse(generate.isEnabled())

    @staticmethod
    def _record(path: Path) -> types.VideoRecord:
        """Build one downloaded row for inspector rendering.

        Example: `_record(path)` supplies title, origin, and local state.
        """

        ident = types.VideoIdentity("aaaaaaaaaaa", "https://youtu.be/aaaaaaaaaaa", "Example")
        origin = types.VideoOrigin("Example channel", "UC-example", 1)
        details = types.VideoDetails(180, 10, "Description", None, "not_live")
        local = types.LocalMedia(path, 2, path.stat().st_size)
        return types.VideoRecord(types.VideoMeta(ident, origin, details), 1, 1, local, None, None)


if __name__ == "__main__":
    unittest.main()
