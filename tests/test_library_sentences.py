"""Exercise the real native sentence page and its shared learning statistics.

Example: `python -m unittest tests.test_library_sentences` needs neither personal history nor API credits.
"""

import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore
from PySide6 import QtTest
from PySide6 import QtWidgets
import shiboken6

from tests import practice_fixtures
from tests import sentence_fixtures as fixtures
from yt_whisper_subs import library_db
from yt_whisper_subs import library_practice
from yt_whisper_subs import library_theme
from yt_whisper_subs import practice
from yt_whisper_subs import practice_activity
from yt_whisper_subs import practice_service
from yt_whisper_subs import sentence_service
from yt_whisper_subs import sentence_stats


class SentenceFlowTests(unittest.TestCase):
    """Drive native events against actual SQLite; e.g. viewing progress never starts a paid exercise."""

    @classmethod
    def setUpClass(cls) -> None:
        """Share one native event loop; e.g. Enter follows the installed Qt behavior."""

        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self) -> None:
        """Build a fresh saved vocabulary and offline provider; e.g. failures cannot alter the user's catalog."""

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = library_db.LibraryDb(Path(tmp.name) / "catalog.sqlite3")
        self.db.initialize()
        fixtures.seed(self.db)
        self.provider = fixtures.FakeSentenceProvider()
        self.engine = sentence_service.SentenceService(self.db, self.provider)
        opts = fixtures.options().model_copy(update={"count": 2})
        self.db.set_setting("sentence_options", opts.model_dump_json())
        self.dialog = None
        self.addCleanup(self._close)

    def _close(self) -> None:
        """Drain workers before removing the catalog; e.g. pending grading cannot outlive its test directory."""

        if self.dialog is not None and shiboken6.isValid(self.dialog):
            self.dialog.close()
        QtCore.QThreadPool.globalInstance().waitForDone(5000)
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def _open(self, *, activity=False) -> None:
        """Inject providers before selecting the automatic practice tab; e.g. no real model is contacted."""

        vocab = practice_service.PracticeService(self.db, practice_fixtures.FakeGenerator())
        self.dialog = library_practice.PracticeDialog(SimpleNamespace(db=self.db), engine=vocab)
        self.panel = self.dialog._sentences
        self.panel._engine = self.engine
        self.dialog.setStyleSheet(library_theme.STYLE + library_practice._STYLE)
        self.dialog.setAttribute(QtCore.Qt.WidgetAttribute.WA_DontShowOnScreen)
        self.dialog.show_page(True)
        if not activity:
            self.dialog._tabs.setCurrentIndex(4)
        self.dialog.show()
        self.app.processEvents()

    def _wait(self, predicate) -> None:
        """Wait for observable asynchronous state; e.g. a native correction arrives from a background worker."""

        deadline = time.monotonic() + 5
        while not predicate() and time.monotonic() < deadline:
            QtTest.QTest.qWait(10)
        self.assertTrue(predicate())

    def _answer(self, text: str) -> None:
        """Submit using the real editor event; e.g. one Enter creates one immutable attempt."""

        self.panel._view.card.editor.setPlainText(text)
        QtTest.QTest.keyClick(self.panel._view.card.editor, QtCore.Qt.Key.Key_Return)

    def test_auto_question_feedback_and_separate_continue(self) -> None:
        """Start with source-only English and reveal Dutch after submission; e.g. a fresh gesture advances after feedback."""

        self._open()
        self._wait(lambda: self.panel._run.row is not None)
        card, first = self.panel._view.card, self.panel._run.row["id"]
        self.assertEqual(card._source.text(), fixtures.ENGLISH)
        self.assertFalse(card.meaning.isVisible())
        self.assertEqual((card._correction.text(), card._translation.text(), card._targets.text()), ("", "", ""))
        self._answer(fixtures.DUTCH.replace("slimste", "slimmere"))
        self._wait(lambda: self.panel._run.row["outcome"] == "wrong")
        self.assertEqual(self.panel._run.row["id"], first)
        self.assertIn("superlative", card._details.text())
        self.assertEqual(card._correction.text(), fixtures.DUTCH)
        self.assertEqual(card._translation.text(), fixtures.ENGLISH)
        self.assertIn("slimste", card._targets.text())
        self.panel._view.action.click()
        self.assertEqual(self.panel._run.row["id"], first)
        QtTest.QTest.qWait(QtWidgets.QApplication.doubleClickInterval() + 30)
        QtTest.QTest.keyClick(self.panel._view.action, QtCore.Qt.Key.Key_Return)
        self.assertNotEqual(self.panel._run.row["id"], first)
        self.assertEqual(card.editor.toPlainText(), "")
        self.assertEqual(practice_activity.read(self.db).answers, 1)

    def test_both_directions_reach_main_progress_and_activity(self) -> None:
        """Integrate writing and comprehension into normal analytics; e.g. a typed sentence counts once despite three saved targets."""

        for direction in (practice.Direction.EN_NL, practice.Direction.NL_EN):
            opts = fixtures.options(direction=direction)
            self.db.set_setting("sentence_options", opts.model_dump_json())
            self.engine.start(opts, lambda _: None)
            self._open()
            self._wait(lambda: self.panel._run.row is not None)
            source = fixtures.ENGLISH if direction == practice.Direction.EN_NL else fixtures.DUTCH
            answer = fixtures.DUTCH if direction == practice.Direction.EN_NL else fixtures.ENGLISH
            self.assertEqual(self.panel._view.card._source.text(), source)
            self.assertFalse(self.panel._view.card.meaning.isVisible())
            self._answer(answer)
            self._wait(lambda: self.panel._run.row["outcome"] == "correct")
            self.dialog._tabs.setCurrentIndex(1)
            self.assertEqual(self.dialog._progress_tabs.currentIndex(), 2)
            self.assertIn("submitted", self.dialog._sentence_progress._totals.text())
            self.assertGreater(self.dialog._sentence_progress._tabs.tables["Forms"].rowCount(), 0)
            self._close()
        data = practice_activity.read(self.db)
        self.assertEqual((data.answers, data.week[-1].sentences, data.distinct_words), (2, 2, 3))
        self.assertEqual((data.week[-1].sentence_en_nl, data.week[-1].sentence_nl_en), (1, 1))

    def test_statistics_only_never_generate_or_expose(self) -> None:
        """Keep analytics read-only; e.g. inspecting sentence forms does not automatically start a round."""

        self._open(activity=True)
        self.dialog._tabs.setCurrentIndex(1)
        self.dialog._progress_tabs.setCurrentIndex(2)
        QtTest.QTest.qWait(100)
        self.assertEqual((self.provider.calls, self.provider.graded), ([], []))
        self.assertIsNone(self.db.sentence_active())
        self.assertEqual(sentence_stats.snapshot(self.db)["shown"], 0)

    def test_pending_correction_resumes_original_answer(self) -> None:
        """Do not turn network errors into learning failures; e.g. reopening retries the saved first attempt exactly once."""

        self._open()
        self._wait(lambda: self.panel._run.row is not None)
        self.provider.available = False
        text = fixtures.DUTCH.replace("slimste", "slimmere")
        self._answer(text)
        self._wait(lambda: self.panel._task is None)
        self.assertEqual(self.panel._view.action.text(), "Retry checking")
        self.assertEqual(sentence_stats.snapshot(self.db)["wrong"], 0)
        self._close()
        self._open()
        self._wait(lambda: self.panel._run.row is not None)
        self.assertEqual(self.panel._view.card.editor.toPlainText(), text)
        self.assertTrue(self.panel._view.card.editor.isReadOnly())
        self.provider.available = True
        self.panel._view.action.click()
        self._wait(lambda: self.panel._run.row["outcome"] == "wrong")
        self.assertEqual(practice_activity.read(self.db).answers, 1)
