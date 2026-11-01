"""Exercise native vocabulary counts, grouping, and occurrence replay without network.

Example: `python -m unittest tests.test_library_vocabulary` uses Qt offscreen.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore
from PySide6 import QtTest
from PySide6 import QtWidgets

from yt_whisper_subs import library_service
from yt_whisper_subs import library_theme
from yt_whisper_subs import library_vocabulary
from yt_whisper_subs import vocabulary as words
from yt_whisper_subs import vocabulary_translate


class VocabularyDialogTests(unittest.TestCase):
    """Verify the displayed view reflects the relational history; e.g. repeated inflections."""

    @classmethod
    def setUpClass(cls) -> None:
        """Keep one QApplication for all native widget tests; e.g. queued selection signals."""

        cls._app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self) -> None:
        """Seed real captured events with deterministic translations; e.g. two source videos."""

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self._service = library_service.LibraryService(root, {"python": Path(sys.executable)}, feed=mock.Mock())
        self._service.vocabulary = mock.Mock()
        self._translator = vocabulary_translate.WordTranslator()
        for idx, (surface, title) in enumerate((("liep", "Dutch news"), ("liep", "Dutch conversation"), ("lopen", "Dutch news"))):
            text = f"Ik {surface} naar huis."
            token = words.Token(surface, 3, 3 + len(surface))
            cue = words.Cue(1000, 4000, text, [token])
            ctx = words.Context("nl", cue, token, "", "", "I walked home.")
            source = words.Source(title, title, title, "https://youtu.be/example", str(root / "sample.mkv"))
            selection = words.Selection(source, ctx, 2, time.time() + idx, str(idx))
            self._service.db.save_word(selection, self._translator.cache_identity(ctx))
        while row := self._service.db.claim_word_lookup("test"):
            meaning = words.Meaning("walked", "lopen", "to walk", "verb", "Past tense", "", False)
            self._service.db.finish_word_lookup(row["id"], "test", meaning)
        self._dialog = library_vocabulary.VocabularyDialog(self._service)
        self._dialog.setStyleSheet(library_theme.STYLE)
        self.addCleanup(self._dialog.close)

    def test_counts_grouping_and_immutable_occurrence_replay(self) -> None:
        """Show both click and video counts, then group inflections without losing history."""

        dialog = self._dialog
        self.assertEqual(dialog._words.rowCount(), 2)
        dialog._group.setChecked(True)
        self.assertEqual(dialog._words.rowCount(), 1)
        self.assertEqual(dialog._words.item(0, 4).data(QtCore.Qt.ItemDataRole.DisplayRole), 3)
        self.assertEqual(dialog._words.item(0, 5).data(QtCore.Qt.ItemDataRole.DisplayRole), 2)
        dialog._words.selectRow(0)
        self.assertEqual(dialog._history.rowCount(), 3)
        dialog._history.selectRow(1)
        selected = dialog._selected_event()
        calls = []
        dialog.play_requested.connect(calls.append)
        QtTest.QTest.mouseClick(dialog._play, QtCore.Qt.MouseButton.LeftButton)
        self.assertEqual(calls, [selected])
        self.assertEqual(calls[0]["title"], "Dutch conversation")
        self.assertIn("Ik liep naar huis.", dialog._detail.toPlainText())
        dialog.refresh()
        self.assertEqual(dialog._selected_event()["id"], selected["id"])

    def test_search_and_retry_do_not_change_history(self) -> None:
        """Filter derived rows and reschedule failed lookups; e.g. no artificial extra clicks."""

        dialog = self._dialog
        dialog._search.setText("nothing matches")
        self.assertEqual(dialog._words.rowCount(), 0)
        self.assertEqual(dialog._history.rowCount(), 0)
        dialog._search.clear()
        self.assertEqual(dialog._words.rowCount(), 2)
        dialog._retry()
        self._service.vocabulary.retry.assert_called_once_with()
        self.assertEqual(sum(row["clicks"] for row in self._service.db.word_summary()), 3)

    @mock.patch("yt_whisper_subs.playback.play_video")
    def test_replay_finds_an_active_source_outside_catalog(self, play) -> None:
        """Reuse an already opened CLI-only source; e.g. avoid duplicate replay windows."""

        event = self._service.db.word_history("nl", "liep")[0]
        event.update(video_id="second", source_key="youtube:second")
        self._service._playback = mock.Mock()
        self._service._playback.seek.side_effect = (False, True)
        self._service.play_occurrence(event)
        self.assertEqual(self._service._playback.seek.call_args_list,
                         [mock.call("second", 1), mock.call("youtube:second", 1)])
        play.assert_not_called()

    def test_render_preview(self) -> None:
        """Optionally capture the actual widget for visual QA; e.g. a review artifact."""

        if path := os.environ.get("YTW_VOCAB_PREVIEW"):
            self._dialog.setAttribute(QtCore.Qt.WidgetAttribute.WA_DontShowOnScreen)
            self._dialog._group.setChecked(True)
            self._dialog._words.selectRow(0)
            self._dialog.show()
            self._app.processEvents()
            self.assertTrue(self._dialog.grab().save(path))
