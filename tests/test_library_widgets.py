"""Tests for the native chapter inspector's state and jump signal.

Example: `python -m unittest tests.test_library_widgets`.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets  # noqa: E402

from yt_whisper_subs import chapters  # noqa: E402
from yt_whisper_subs import library_chapter_actions  # noqa: E402
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

    def test_chapter_action_seeks_matching_active_player(self) -> None:
        """Prefer the running mpv process without entering the busy task lane.

        Example: a chapter double-click moves the already visible player.
        """

        record = self._record(Path(__file__))
        window = mock.Mock()
        window._selected_record.return_value = record
        window._service.seek.return_value = True
        window._ui = SimpleNamespace(trace=mock.Mock())

        library_chapter_actions.ChapterActionsMixin._play_chapter(window, 90.0)

        window._service.seek.assert_called_once_with("aaaaaaaaaaa", 90.0)
        window._play_from.assert_not_called()
        window.statusBar().showMessage.assert_called_once_with(
            "Seek → 1:30 · Example",
            3_000,
        )
        window._ui.trace.append_message.assert_called_once()

    def test_chapter_action_launches_when_no_matching_player(self) -> None:
        """Retain launch-at-time behavior when the selected video is not active.

        Example: a stopped video opens directly at the chosen chapter.
        """

        window = mock.Mock()
        window._selected_record.return_value = self._record(Path(__file__))
        window._service.seek.return_value = False

        library_chapter_actions.ChapterActionsMixin._play_chapter(window, 90.0)

        window._play_from.assert_called_once_with(90.0)

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
