"""Native practice flows, resumable rounds, and rendered progress with isolated data.

Example: `python -m unittest tests.test_library_practice` uses an injected fake question provider.
"""

from __future__ import annotations

from contextlib import ExitStack
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
from yt_whisper_subs import practice_tracks


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
        self.quiz = dialog._quizzes[practice.Direction.NL_EN]
        dialog.setStyleSheet(library_theme.STYLE + library_practice._STYLE)
        dialog.setAttribute(QtCore.Qt.WidgetAttribute.WA_DontShowOnScreen)
        dialog.show_page(True)
        dialog.show()
        self.app.processEvents()
        return dialog

    def _wait_ready(self) -> None:
        """Wait for the worker's meaningful completion; e.g. avoid sleep-only assertions about API preparation."""

        deadline = time.monotonic() + 10
        while self.quiz._current is None:
            QtTest.QTest.qWait(10)
            if time.monotonic() > deadline:
                self.fail(self.quiz._status.text())

    def test_answer_report_and_next_use_real_saved_history(self) -> None:
        """A wrong answer highlights feedback, reporting removes it, and Next presents a different word."""

        self.quiz._controls.count.setValue(3)
        self.dialog.show_page(False)
        self._wait_ready()
        row = self.quiz._current
        wrong = (row["correct_idx"] + 1) % 3
        QtTest.QTest.mouseClick(self.quiz._answers._buttons[wrong], QtCore.Qt.MouseButton.LeftButton)
        self.assertIn("Not quite", self.quiz._result.text())
        self.assertTrue(all(button.isEnabled() for button in self.quiz._answers._buttons))
        self.assertEqual(practice_stats.snapshot(self.db)["totals"]["wrong"], 1)
        self.quiz._flag.click()
        self.assertEqual(practice_stats.snapshot(self.db)["totals"]["attempts"], 0)
        QtTest.QTest.qWait(QtWidgets.QApplication.doubleClickInterval() + 20)
        self.quiz._next.click()
        self.assertNotEqual(row["word_id"], self.quiz._current["word_id"])
        QtTest.QTest.qWait(QtWidgets.QApplication.doubleClickInterval() + 20)
        self.quiz._unsure.setChecked(True)
        self.quiz._answers._buttons[self.quiz._current["correct_idx"]].click()
        self.assertEqual(self.quiz._current["rating"], 2)
        self.quiz._controls.finish_requested.emit()
        self.assertIn("Round saved", self.quiz._prompt.text())
        self.dialog._tabs.setCurrentIndex(library_practice.Page.PROGRESS)
        self.assertGreater(self.dialog._progress._terms.rowCount(), 0)

    def test_insufficient_targets_offer_options_without_a_partial_round(self) -> None:
        """Keep unavailable sizes recoverable in the native flow; e.g. four saved terms cannot silently turn a 35-question request into four."""

        self.quiz._controls.count.setValue(35)
        self.dialog.show_page(False)
        deadline = time.monotonic() + 5
        while self.quiz._task is not None:
            QtTest.QTest.qWait(10)
            if time.monotonic() > deadline:
                self.fail("Round availability did not resolve")
        self.assertEqual(self.quiz._prompt.text(), "Choose a smaller round")
        self.assertIsNone(self.quiz._current)
        self.assertIsNone(self.db.practice_active())

    def test_grouped_navigation_separates_practice_from_tracking_without_extra_steps(self) -> None:
        """Use actual grouped page buttons and native page state; e.g. visiting Progress records no exposure, then one Word quiz click opens a question."""

        self.assertEqual([self.dialog._tabs.tabText(idx) for idx in range(10)],
                         ["Word quiz\nDutch → English", "Word quiz\nEnglish → Dutch", "Verbs\nDutch transformations",
                          "Sentences\nEnglish → Dutch", "Sentences\nDutch → English", "Listening\nDutch dictation",
                          'Reading comprehension\nText and questions', 'Listening comprehension\nText and questions', "Progress", "Activity"])
        self.assertFalse(self.dialog._tabs.tabBar().isVisible())
        labels = {label.text() for label in self.dialog._nav.findChildren(QtWidgets.QLabel)}
        self.assertEqual(labels, {"PRACTICE", "TRACKING"})
        buttons = self.dialog._nav._buttons
        buttons.button(library_practice.Page.PROGRESS).click()
        self.assertEqual(self.dialog._tabs.currentIndex(), library_practice.Page.PROGRESS)
        self.assertIsNone(self.quiz._current)
        self.assertEqual(practice_stats.snapshot(self.db)["totals"]["shown"], 0)
        QtTest.QTest.keyClick(self.dialog._tabs, QtCore.Qt.Key.Key_Tab, QtCore.Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.dialog._tabs.currentIndex(), library_practice.Page.ACTIVITY)
        self.assertTrue(buttons.button(library_practice.Page.ACTIVITY).isChecked())
        self.app.processEvents()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        buttons.button(library_practice.Page.WORD_NL_EN).click()
        self._wait_ready()
        self.assertEqual(practice_stats.snapshot(self.db)["totals"]["shown"], 1)
        self.dialog.show_page(True)
        self.assertTrue(buttons.button(library_practice.Page.ACTIVITY).isChecked())
        self.assertEqual(practice_stats.snapshot(self.db)["totals"]["shown"], 1)

    def test_close_and_resume_does_not_recount_the_current_question(self) -> None:
        """Closing the dialog preserves the round; e.g. continue later with the same button ordering."""

        self.dialog.show_page(False)
        self._wait_ready()
        self.assertEqual(self.dialog._tabs.tabText(0), "Word quiz\nDutch → English")
        row = self.quiz._current
        self.dialog.close()
        self.app.processEvents()
        self.dialog = self._dialog()
        self.dialog.show_page(False)
        self._wait_ready()
        self.assertEqual(self.quiz._current["id"], row["id"])
        self.assertEqual(self.quiz._current["choices"], row["choices"])
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

        editor = self.quiz._controls.count.lineEdit()
        editor.selectAll()
        QtTest.QTest.keyClicks(editor, "12")
        self.assertEqual(self.quiz._controls.count.value(), 12)

    def test_top_level_direction_buttons_preserve_both_rounds_and_feedback(self) -> None:
        """Switch actual destinations directly; e.g. returning to Dutch-to-English preserves its answer and shuffled choices."""

        buttons = self.dialog._nav._buttons
        buttons.button(library_practice.Page.WORD_NL_EN).click()
        self._wait_ready()
        first = self.quiz._current
        self.quiz._answers._buttons[first["correct_idx"]].click()
        original = self.quiz
        self.quiz = self.dialog._quizzes[practice.Direction.EN_NL]
        buttons.button(library_practice.Page.WORD_EN_NL).click()
        self._wait_ready()
        self.assertEqual(self.quiz._current["direction"], "en_nl")
        self.assertNotEqual(self.quiz._session, original._session)
        buttons.button(library_practice.Page.WORD_NL_EN).click()
        self.assertEqual(original._current["id"], first["id"])
        self.assertEqual(original._current["choices"], first["choices"])
        self.assertEqual(original._current["outcome"], "correct")
        self.assertEqual(practice_stats.snapshot(self.db)["totals"]["shown"], 2)

    def test_full_practice_moves_on_after_feedback_and_resumes_after_close(self) -> None:
        """Use real single-question rounds to exercise the first two modes and a close at the handoff."""

        for quiz in self.dialog._quizzes.values():
            quiz._controls.count.setValue(1)
        self.dialog._sequence._start.click()
        self.assertEqual(self.dialog._tabs.currentIndex(), library_practice.Page.WORD_NL_EN)
        self._wait_ready()
        self.quiz._answers._buttons[self.quiz._current['correct_idx']].click()
        self.assertEqual(self.dialog._sequence.current, practice_tracks.Track.WORD_NL_EN)
        QtTest.QTest.qWait(QtWidgets.QApplication.doubleClickInterval() + 20)
        self.quiz._next.click()
        self.assertEqual(self.dialog._tabs.currentIndex(), library_practice.Page.WORD_EN_NL)
        self.quiz = self.dialog._quizzes[practice.Direction.EN_NL]
        self._wait_ready()
        self.quiz._answers._buttons[self.quiz._current['correct_idx']].click()
        self._close()

        self.dialog = self._dialog()
        self.assertEqual(self.dialog._sequence._start.text(), 'Resume full practice')
        with mock.patch.object(self.dialog._verbs, 'activate'), mock.patch.object(self.dialog._verbs, 'enter_sequence'):
            self.dialog.show_page(sequence=True)
            self.app.processEvents()
        self.assertEqual(self.dialog._tabs.currentIndex(), library_practice.Page.VERBS)
        self.assertIn('3/8', self.dialog._sequence._status.text())
        self.assertEqual(practice_stats.snapshot(self.db)['totals']['attempts'], 2)

    def test_full_practice_survives_navigation_and_visits_every_mode_in_order(self) -> None:
        """Drive all eight handoffs, including skipping an unavailable mode and pausing to see Activity."""

        tracks = list(practice_tracks.Track)
        bar = self.dialog._sequence
        with ExitStack() as stack:
            for panel in self.dialog._panels.values():
                stack.enter_context(mock.patch.object(panel, 'activate'))
                stack.enter_context(mock.patch.object(panel, 'enter_sequence'))
            bar._start.click()
            for idx, track in enumerate(tracks):
                self.app.processEvents()
                self.assertEqual(bar.current, track)
                self.assertEqual(self.dialog._tabs.currentIndex(), library_practice.Page.for_track(track))
                if idx == 2:
                    self.dialog.show_page(True)
                    self.assertIsNone(bar.current)
                    bar._start.click()
                    self.assertEqual(bar.current, track)
                    bar._skip.click()
                else:
                    self.dialog._panels[library_practice.Page.for_track(track)].round_reviewed.emit()
            self.app.processEvents()
        self.assertIsNone(bar.current)
        self.assertIn('7 completed · 1 skipped', bar._status.text())
        self.assertEqual(practice_stats.snapshot(self.db)['totals']['attempts'], 0)

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
        self.quiz._task.signals.cancelled.connect(lambda: cancelled.append(True))
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
        self.quiz = self.dialog._quizzes[practice.Direction.EN_NL]
        self.quiz._controls.early.setChecked(True)
        self.dialog._tabs.setCurrentIndex(library_practice.Page.WORD_EN_NL)
        self._wait_ready()
        self.assertNotIn(self.quiz._current["content"]["surface"], self.quiz._context.text())
        if folder := os.environ.get("YTW_PRACTICE_PREVIEW"):
            self.app.processEvents()
            self.assertTrue(self.dialog.grab().save(str(Path(folder) / "practice-round.png")))
        self.quiz._answers._buttons[self.quiz._current["correct_idx"]].click()
        self.assertIn(self.quiz._current["content"]["surface"], self.quiz._result.text())
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
