"""Native practice flows, resumable rounds, and rendered progress with isolated data.

Example: `python -m unittest tests.test_library_practice` uses an injected fake question provider.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore
from PySide6 import QtGui
from PySide6 import QtTest
from PySide6 import QtWidgets
import shiboken6

from tests import practice_fixtures
from yt_whisper_subs import library_db
from yt_whisper_subs import library_practice
from yt_whisper_subs import library_practice_controls
from yt_whisper_subs import library_theme
from yt_whisper_subs import practice
from yt_whisper_subs import practice_service
from yt_whisper_subs import practice_stats


class PracticeDialogTests(unittest.TestCase):
    """Exercise large native buttons and saved outcomes rather than implementation-only layout assertions."""

    @classmethod
    def setUpClass(cls) -> None:
        """Share a Qt event loop with other desktop tests; e.g. background preparation replies are queued."""

        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self) -> None:
        """Build a complete catalog and injectable strategy; e.g. no API key or user's database is required."""

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = library_db.LibraryDb(Path(tmp.name) / "catalog.sqlite3")
        self.db.initialize()
        practice_fixtures.seed(self.db)
        opts = practice.RoundOptions(direction=practice.Direction.NL_EN, count=4)
        self.db.set_setting("practice_options", opts.model_dump_json())
        self.engine = practice_service.PracticeService(self.db, practice_fixtures.FakeGenerator())
        self.dialog = self._dialog()
        self.addCleanup(self._close)

    def _close(self) -> None:
        """Drain owned work before removing its temporary catalog; e.g. a dialog already deleted by closing cannot be closed twice."""

        if shiboken6.isValid(self.dialog):
            self.dialog.close()
        QtCore.QThreadPool.globalInstance().waitForDone(10000)
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def _dialog(self):
        """Render actual native widgets without showing desktop windows; e.g. use Windows font rendering for previews."""

        dialog = library_practice.PracticeDialog(SimpleNamespace(db=self.db), engine=self.engine)
        dialog.setStyleSheet(library_theme.STYLE + library_practice._STYLE)
        dialog.setAttribute(QtCore.Qt.WidgetAttribute.WA_DontShowOnScreen)
        dialog.show_page(True)
        dialog.show()
        self.app.processEvents()
        return dialog

    def _wait_ready(self) -> None:
        """Wait for the worker's meaningful completion; e.g. avoid sleep-only assertions about API preparation."""

        deadline = time.monotonic() + 10
        while self.dialog._current is None:
            QtTest.QTest.qWait(10)
            if time.monotonic() > deadline:
                self.fail(self.dialog._status.text())

    def test_answer_report_and_next_use_real_saved_history(self) -> None:
        """A wrong answer highlights feedback, reporting removes it, and Next presents a different word."""

        self.dialog._controls.count.setValue(3)
        self.dialog.show_page(False)
        self._wait_ready()
        row = self.dialog._current
        wrong = (row["correct_idx"] + 1) % 3
        QtTest.QTest.mouseClick(self.dialog._answers._buttons[wrong], QtCore.Qt.MouseButton.LeftButton)
        self.assertIn("Not quite", self.dialog._result.text())
        self.assertTrue(all(button.isEnabled() for button in self.dialog._answers._buttons))
        self.assertEqual(practice_stats.snapshot(self.db)["totals"]["wrong"], 1)
        self.dialog._flag.click()
        self.assertEqual(practice_stats.snapshot(self.db)["totals"]["attempts"], 0)
        QtTest.QTest.qWait(QtWidgets.QApplication.doubleClickInterval() + 20)
        self.dialog._next.click()
        self.assertNotEqual(row["word_id"], self.dialog._current["word_id"])
        QtTest.QTest.qWait(QtWidgets.QApplication.doubleClickInterval() + 20)
        self.dialog._unsure.setChecked(True)
        self.dialog._answers._buttons[self.dialog._current["correct_idx"]].click()
        self.assertEqual(self.dialog._current["rating"], 2)
        self.dialog._controls.finish_requested.emit()
        self.assertIn("Round saved", self.dialog._prompt.text())
        self.dialog._tabs.setCurrentIndex(library_practice.Page.PROGRESS)
        self.assertGreater(self.dialog._progress._terms.rowCount(), 0)

    def test_insufficient_targets_offer_options_without_a_partial_round(self) -> None:
        """Keep unavailable sizes recoverable in the native flow; e.g. four saved terms cannot silently turn a 35-question request into four."""

        self.dialog._controls.count.setValue(35)
        self.dialog.show_page(False)
        deadline = time.monotonic() + 5
        while self.dialog._task is not None:
            QtTest.QTest.qWait(10)
            if time.monotonic() > deadline:
                self.fail("Round availability did not resolve")
        self.assertEqual(self.dialog._prompt.text(), "Choose a smaller round")
        self.assertIsNone(self.dialog._current)
        self.assertIsNone(self.db.practice_active())

    def test_grouped_navigation_separates_practice_from_tracking_without_extra_steps(self) -> None:
        """Use actual grouped page buttons and native page state; e.g. visiting Progress records no exposure, then one Word quiz click opens a question."""

        self.assertEqual([self.dialog._tabs.tabText(idx) for idx in range(5)], ["Word quiz", "Verbs", "Sentences", "Progress", "Activity"])
        self.assertFalse(self.dialog._tabs.tabBar().isVisible())
        labels = {label.text() for label in self.dialog._nav.findChildren(QtWidgets.QLabel)}
        self.assertEqual(labels, {"PRACTICE", "TRACKING"})
        buttons = self.dialog._nav._buttons
        buttons.button(library_practice.Page.PROGRESS).click()
        self.assertEqual(self.dialog._tabs.currentIndex(), library_practice.Page.PROGRESS)
        self.assertIsNone(self.dialog._current)
        self.assertEqual(practice_stats.snapshot(self.db)["totals"]["shown"], 0)
        QtTest.QTest.keyClick(self.dialog._tabs, QtCore.Qt.Key.Key_Tab, QtCore.Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.dialog._tabs.currentIndex(), library_practice.Page.ACTIVITY)
        self.assertTrue(buttons.button(library_practice.Page.ACTIVITY).isChecked())
        self.app.processEvents()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        buttons.button(library_practice.Page.QUIZ).click()
        self._wait_ready()
        self.assertEqual(practice_stats.snapshot(self.db)["totals"]["shown"], 1)
        self.dialog.show_page(True)
        self.assertTrue(buttons.button(library_practice.Page.ACTIVITY).isChecked())
        self.assertEqual(practice_stats.snapshot(self.db)["totals"]["shown"], 1)

    def test_close_and_resume_does_not_recount_the_current_question(self) -> None:
        """Closing the dialog preserves the round; e.g. continue later with the same button ordering."""

        self.dialog.show_page(False)
        self._wait_ready()
        self.assertEqual(self.dialog._tabs.tabText(0), "Word quiz")
        row = self.dialog._current
        self.dialog.close()
        self.app.processEvents()
        self.dialog = self._dialog()
        self.dialog.show_page(False)
        self._wait_ready()
        self.assertEqual(self.dialog._current["id"], row["id"])
        self.assertEqual(self.dialog._current["choices"], row["choices"])
        self.assertEqual(practice_stats.snapshot(self.db)["totals"]["shown"], 1)

    def test_example_emphasizes_whole_words_inflections_and_hidden_answer_gaps(self) -> None:
        """Inspect actual rich-text rendering and answer secrecy; e.g. liep pops while liepen and literal HTML remain ordinary text."""

        scenarios = [
            ("nl_en", "liep", "Liep, liep! Liepen. <b>woord</b> & fiets", [0, 4], ["Liep", "liep"]),
            ("nl_en", "veilig", "Deze route is veiliger.", [14, 22], ["veiliger"]),
            ("nl_en", "de", "de deur in deze kamer", [0, 2], ["de"]),
            ("en_nl", "liep", "Ik ____ en ____.", [3, 7], ["____", "____"]),
        ]
        for direction, word, sentence, span, expected in scenarios:
            with self.subTest(word=word, direction=direction):
                label = library_practice_controls.QuizContext()
                label.present({"direction": direction, "content": {"context": sentence, "surface": word},
                               "lookup_context": {"sentence": sentence, "span": span}})
                document = QtGui.QTextDocument()
                document.setHtml(label.text())
                self.assertEqual(document.toPlainText(), sentence)
                highlighted = []
                block = document.begin()
                while block.isValid():
                    cursor = block.begin()
                    while not cursor.atEnd():
                        fragment = cursor.fragment()
                        if fragment.charFormat().fontWeight() >= QtGui.QFont.Weight.Bold:
                            highlighted.append(fragment.text())
                        cursor += 1
                    block = block.next()
                self.assertEqual(highlighted, expected)
                if direction == "en_nl":
                    self.assertNotIn(word, document.toPlainText())
                label.message("<b>Could not prepare & retry</b>")
                document.setHtml(label.text())
                self.assertEqual(document.toPlainText(), "<b>Could not prepare & retry</b>")

    def test_answer_shortcuts_do_not_intercept_round_length_entry(self) -> None:
        """Numeric answer access must respect native editing; e.g. type a twelve-term round before starting."""

        editor = self.dialog._controls.count.lineEdit()
        editor.selectAll()
        QtTest.QTest.keyClicks(editor, "12")
        self.assertEqual(self.dialog._controls.count.value(), 12)

    def test_closing_during_generation_cancels_without_a_partial_round(self) -> None:
        """A delayed provider must not hold the window open or invent exposures; e.g. close during an API call."""

        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        generate = self.engine._provider.generate

        def blocked(rows, direction):
            """Simulate an uninterruptible HTTP segment; e.g. cancellation is checked at the next report."""

            entered.set()
            release.wait(20)
            return generate(rows, direction)

        self.engine._provider.generate = blocked
        self.dialog.show_page(False)
        deadline = time.monotonic() + 10
        while not entered.is_set() and time.monotonic() < deadline:
            QtTest.QTest.qWait(10)
        self.assertTrue(entered.is_set())
        cancelled = []
        self.dialog._task.signals.cancelled.connect(lambda: cancelled.append(True))
        self.dialog.close()
        release.set()
        deadline = time.monotonic() + 10
        while not cancelled and time.monotonic() < deadline:
            QtTest.QTest.qWait(10)
        self.assertTrue(cancelled)
        self.assertIsNone(self.db.practice_active())
        self.assertEqual(practice_stats.snapshot(self.db)["totals"]["shown"], 0)
        self.dialog = self._dialog()

    def test_progress_renders_multiple_days_and_both_directions(self) -> None:
        """Plot actual saved time series and an unanswered reverse card; e.g. visually inspect a realistic study history."""

        origin = time.time() - 6 * 86400
        for day in range(6):
            for direction in practice.Direction:
                with mock.patch("yt_whisper_subs.practice_db.time.time", return_value=origin + day * 86400):
                    opts = practice.RoundOptions(direction=direction, count=4, include_early=True)
                    session = self.engine.start(opts, lambda _: None)
                    while row := self.db.practice_current(session):
                        choice = row["correct_idx"] if (row["ordinal"] + day) % 4 else (row["correct_idx"] + 1) % 3
                        self.db.practice_answer(row["id"], choice, False, 1800 + row["ordinal"] * 550)
                    self.db.practice_finish(session)
        data = practice_stats.snapshot(self.db)
        self.assertEqual(len(data["daily"]), 12)
        self.assertEqual(data["coverage"][-1]["words"], 4)
        self.assertEqual(data["totals"]["attempts"], 48)
        self.dialog._controls.direction.setCurrentIndex(1)
        self.dialog._controls.early.setChecked(True)
        self.dialog.show_page(False)
        self._wait_ready()
        self.assertNotIn(self.dialog._current["content"]["surface"], self.dialog._context.text())
        if folder := os.environ.get("YTW_PRACTICE_PREVIEW"):
            self.app.processEvents()
            self.assertTrue(self.dialog.grab().save(str(Path(folder) / "practice-round.png")))
        self.dialog._answers._buttons[self.dialog._current["correct_idx"]].click()
        self.assertIn(self.dialog._current["content"]["surface"], self.dialog._result.text())
        self.dialog._tabs.setCurrentIndex(library_practice.Page.PROGRESS)
        self.app.processEvents()
        self.assertEqual(self.dialog._progress._terms.rowCount(), 8)
        self.assertEqual(self.dialog._progress._rounds.rowCount(), 13)
        self.dialog._progress._terms.sortItems(5, QtCore.Qt.SortOrder.AscendingOrder)
        table = self.dialog._progress._terms
        keys = [table.item(idx, 5).data(QtCore.Qt.ItemDataRole.UserRole) for idx in range(table.rowCount())]
        self.assertEqual(keys, sorted(keys))
        if folder:
            self.assertTrue(self.dialog.grab().save(str(Path(folder) / "practice-progress.png")))
