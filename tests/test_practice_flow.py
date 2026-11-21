"""Verify the actual click/keyboard learning loop against real saved rounds.

Example: `python -m unittest tests.test_practice_flow` uses an offline question provider.
"""

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
from yt_whisper_subs import library_theme
from yt_whisper_subs import practice
from yt_whisper_subs import practice_service
from yt_whisper_subs import practice_stats


class PracticeFlowTests(unittest.TestCase):
    """Exercise deliberate gestures and visible exposures; e.g. an answered card advances without regrading."""

    @classmethod
    def setUpClass(cls) -> None:
        """Share native Qt dispatch; e.g. keyboard default buttons must behave like the real application."""

        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self) -> None:
        """Keep each round and cache isolated; e.g. no API or user database is touched."""

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = library_db.LibraryDb(Path(tmp.name) / "catalog.sqlite3")
        self.db.initialize()
        practice_fixtures.seed(self.db)
        self.generator = practice_fixtures.FakeGenerator()
        self.engine = practice_service.PracticeService(self.db, self.generator)
        self.dialog = None
        self.addCleanup(self._close)

    def _close(self) -> None:
        """Stop background preparation before deleting files; e.g. a failed test cannot strand a task."""

        if self.dialog is not None and shiboken6.isValid(self.dialog):
            self.dialog.close()
        QtCore.QThreadPool.globalInstance().waitForDone(5000)
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def _open(self, *, activity: bool = False, count: int = 3) -> None:
        """Open a genuine dialog with saved defaults; e.g. no setup/start action is necessary."""

        opts = practice.RoundOptions(direction=practice.Direction.NL_EN, count=count)
        self.db.set_setting("practice_options", opts.model_dump_json())
        self.dialog = library_practice.PracticeView(SimpleNamespace(db=self.db), engine=self.engine)
        self.dialog.resize(1160, 900)
        self.dialog.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        self.quiz = self.dialog._quizzes[practice.Direction.NL_EN]
        self.dialog.setStyleSheet(library_theme.STYLE + library_practice._STYLE)
        self.dialog.setAttribute(QtCore.Qt.WidgetAttribute.WA_DontShowOnScreen)
        self.dialog.show_page(activity)
        self.dialog.show()
        self.app.processEvents()

    def _wait(self, predicate) -> None:
        """Wait for an observable state with event dispatch; e.g. a prepared first question or cancellation signal."""

        deadline = time.monotonic() + 10
        while not predicate() and time.monotonic() < deadline:
            QtTest.QTest.qWait(10)
        if not predicate():
            state = {"prompt": self.quiz._prompt.text(), "message": self.quiz._context.text(), "preparing": self.quiz._task is not None}
            self.fail(str(state))

    def _ready(self) -> None:
        """Wait for a displayed question, not just a worker result; e.g. Activity can hold a prepared unshown round."""

        self._wait(lambda: self.quiz._current is not None)
        # The worker signal sets the row before Qt processes its deferred layout requests.
        self.app.processEvents()

    def _gesture_gap(self) -> None:
        """Allow a deliberate second gesture; e.g. preserve the native double-click protection in tests."""

        QtTest.QTest.qWait(QtWidgets.QApplication.doubleClickInterval() + 25)

    def test_opens_on_first_question_without_start_and_resumes_without_recounting(self) -> None:
        """Opening is sufficient for the first question; e.g. reopening preserves its exact choices and exposure count."""

        self._open()
        self._ready()
        first = self.quiz._current
        self.assertEqual(practice_stats.snapshot(self.db)["totals"]["shown"], 1)
        self._close()
        self._open()
        self._ready()
        self.assertEqual(self.quiz._current["id"], first["id"])
        self.assertEqual(self.quiz._current["choices"], first["choices"])
        self.assertEqual(practice_stats.snapshot(self.db)["totals"]["shown"], 1)

    def test_feedback_click_continues_and_double_clicks_do_not_answer_the_next_word(self) -> None:
        """Require separate gestures on both sides of feedback; e.g. a continuation's second click cannot grade the next word."""

        self._open()
        self._ready()
        first = self.quiz._current
        button = self.quiz._answers._buttons[first["correct_idx"]]
        positions = [button.mapTo(self.dialog, QtCore.QPoint()).y() for button in self.quiz._answers._buttons]
        QtTest.QTest.mouseClick(button, QtCore.Qt.MouseButton.LeftButton)
        self.app.processEvents()
        after = [button.mapTo(self.dialog, QtCore.QPoint()).y() for button in self.quiz._answers._buttons]
        self.assertEqual(positions, after)
        QtTest.QTest.mouseClick(button, QtCore.Qt.MouseButton.LeftButton)
        self.assertEqual(self.quiz._current["id"], first["id"])
        self.assertIn("Correct", self.quiz._result.text())
        self._gesture_gap()
        QtTest.QTest.mouseClick(button, QtCore.Qt.MouseButton.LeftButton)
        second = self.quiz._current
        self.assertNotEqual(second["word_id"], first["word_id"])
        QtTest.QTest.mouseClick(button, QtCore.Qt.MouseButton.LeftButton)
        self.assertIsNone(self.quiz._current["outcome"])
        data = practice_stats.snapshot(self.db)
        self.assertEqual((data["totals"]["attempts"], data["totals"]["shown"]), (1, 2))

    def test_choice_paints_before_save_and_can_retry_after_a_failure(self) -> None:
        """Observe the blue cue before SQLite runs and confirm a failed save leaves the choice retryable."""

        self._open()
        self._ready()
        button = self.quiz._answers._buttons[self.quiz._current['correct_idx']]
        painted = []

        class PaintProbe(QtCore.QObject):
            """Record actual paints, including the one before the answer handler saves."""

            def eventFilter(self, watched, event):
                """Capture the state painted by the answer widget while preserving its normal draw."""

                if event.type() == QtCore.QEvent.Type.Paint:
                    painted.append(watched.property('answerState'))
                return False

        probe = PaintProbe(button)
        button.installEventFilter(probe)

        def failed_save(*_args):
            """Stop at the save boundary and require the button to have painted already."""

            self.assertTrue(painted)
            self.assertEqual(painted[-1], 'pending')
            raise OSError('Test save failure')

        with mock.patch.object(self.db, 'practice_answer', side_effect=failed_save):
            QtTest.QTest.mouseClick(button, QtCore.Qt.MouseButton.LeftButton)
        self.assertEqual(button.property('answerState'), '')
        self.assertIn('Test save failure', self.quiz._result.text())
        self.assertIsNone(self.quiz._current['outcome'])
        QtTest.QTest.mouseClick(button, QtCore.Qt.MouseButton.LeftButton)
        self.assertEqual(button.property('answerState'), 'correct')
        self.assertEqual(practice_stats.snapshot(self.db)['totals']['attempts'], 1)

    def test_card_background_and_keyboard_continue_after_feedback(self) -> None:
        """Use large reading targets and native keys; e.g. Space then Enter need no pointer movement to Next."""

        self._open()
        self._ready()
        row = self.quiz._current
        self.quiz._answers._buttons[row["correct_idx"]].click()
        self._gesture_gap()
        pos = self.quiz._prompt.mapTo(self.quiz._surface, self.quiz._prompt.rect().center())
        QtTest.QTest.mouseClick(self.quiz._surface, QtCore.Qt.MouseButton.LeftButton, pos=pos)
        self.assertNotEqual(self.quiz._current["id"], row["id"])
        for key in (QtCore.Qt.Key.Key_Space, QtCore.Qt.Key.Key_Return):
            self._gesture_gap()
            row = self.quiz._current
            self.quiz._answers._buttons[row["correct_idx"]].click()
            self._gesture_gap()
            QtTest.QTest.keyClick(self.quiz._next, key)
            if key == QtCore.Qt.Key.Key_Space:
                self.assertIsNone(self.quiz._current["outcome"])
            else:
                self.assertIn("Round complete", self.quiz._prompt.text())

    def test_details_reporting_and_numeric_keys_do_not_skip_feedback(self) -> None:
        """Keep auxiliary actions independent; e.g. inspect an explanation or report without advancing."""

        self._open()
        self._ready()
        first = self.quiz._current
        wrong = (first["correct_idx"] + 1) % 3
        self.quiz._answers._buttons[wrong].click()
        self._gesture_gap()
        QtTest.QTest.keyClick(self.quiz._answers._buttons[wrong], QtCore.Qt.Key.Key_1)
        self.quiz._details.click()
        self.assertTrue(self.quiz._explanation.isVisible())
        self.assertEqual(self.quiz._current["id"], first["id"])
        self.quiz._flag.click()
        self.assertEqual(self.quiz._current["id"], first["id"])
        self.assertEqual(practice_stats.snapshot(self.db)["totals"]["attempts"], 0)
        self._gesture_gap()
        self.quiz._next.click()
        self.assertNotEqual(self.quiz._current["id"], first["id"])
        self.assertFalse(self.quiz._explanation.isVisible())

    def test_finished_round_has_one_next_action_and_ignores_held_enter(self) -> None:
        """Stop at the configured round boundary; e.g. holding Enter cannot start another round."""

        self._open(count=1)
        self._ready()
        self.quiz._answers._buttons[self.quiz._current["correct_idx"]].click()
        self._gesture_gap()
        self.quiz._next.click()
        self.assertIsNone(self.quiz._session)
        self.assertEqual(self.quiz._next.text(), "Another round")
        event = QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_Return,
                               QtCore.Qt.KeyboardModifier.NoModifier, "", True)
        self.app.sendEvent(self.quiz._next, event)
        self.assertIsNone(self.quiz._task)
        QtTest.QTest.keyClick(self.quiz._next, QtCore.Qt.Key.Key_Return)
        self._ready()
        self.assertIsNone(self.quiz._current["outcome"])

    def test_opening_activity_does_not_prepare_or_expose_a_question(self) -> None:
        """Keep the analytics entry read-only; e.g. the reminder's Activity link must not generate a practice round."""

        self._open(activity=True)
        self.app.processEvents()
        self.assertIsNone(self.quiz._task)
        self.assertIsNone(self.db.practice_active())
        self.assertEqual(self.generator.calls, [])
        self.dialog.show_page(False)
        self._ready()
        self.assertEqual(practice_stats.snapshot(self.db)["totals"]["shown"], 1)

    def test_preparation_completed_on_activity_waits_for_practice_before_counting(self) -> None:
        """Defer exposure until visible even when work is done; e.g. switch to stats while choices are preparing."""

        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        generate = self.generator.generate

        def blocked(rows, direction):
            """Hold a genuine provider batch so tab switching races are deterministic; e.g. release after opening Activity."""

            entered.set()
            release.wait(3)
            return generate(rows, direction)

        self.generator.generate = blocked
        self._open()
        self._wait(entered.is_set)
        self.dialog.show_page(True)
        release.set()
        self._wait(lambda: self.quiz._task is None)
        self.assertIsNone(self.quiz._current)
        self.assertEqual(practice_stats.snapshot(self.db)["totals"]["shown"], 0)
        calls = len(self.generator.calls)
        self.dialog.show_page(False)
        self._ready()
        self.assertEqual(len(self.generator.calls), calls)

    def test_reopening_cached_questions_does_not_wait_for_variant_generation(self) -> None:
        """Use approved content first on opening; e.g. skip a provider that would take another ninety seconds."""

        opts = practice.RoundOptions(direction=practice.Direction.NL_EN, count=4)
        session = self.engine.start(opts, lambda _: None)
        self.db.practice_current(session)
        self.db.practice_finish(session)
        generate = mock.Mock(side_effect=AssertionError("opening must not replenish variants"))
        self.generator.generate = generate
        self._open(count=4)
        self._ready()
        generate.assert_not_called()

    def test_all_caught_up_offers_explicit_early_review(self) -> None:
        """Keep scheduling intact until the user opts in; e.g. do not silently fill daily rounds with future words."""

        opts = practice.RoundOptions(direction=practice.Direction.NL_EN, count=4)
        session = self.engine.start(opts, lambda _: None)
        while row := self.db.practice_current(session):
            self.db.practice_answer(row["id"], row["correct_idx"], False, 1200)
        self._open()
        self._wait(lambda: self.quiz._task is None)
        self.assertIn("caught up", self.quiz._prompt.text())
        self.assertEqual(self.quiz._next.text(), "Practice early")
        self.quiz._next.click()
        self._ready()
        self.assertFalse(self.quiz._controls.early.isChecked())
        saved = practice.RoundOptions.model_validate_json(self.db.setting("practice_options", ""))
        self.assertFalse(saved.include_early)

    def test_failed_restart_can_resume_original_question(self) -> None:
        """Keep the original round intact if replacement preparation fails; e.g. an offline provider cannot lose a question."""

        self._open(count=1)
        self._ready()
        first = self.quiz._current
        self.generator.available = False
        self.quiz._controls.count.setValue(4)
        self.quiz._controls.restart_requested.emit()
        self._wait(lambda: self.quiz._task is None)
        self.assertIn("Couldn't prepare", self.quiz._prompt.text())
        self.assertEqual(self.quiz._next.text(), "Resume round")
        self.quiz._next.click()
        self.assertEqual(self.quiz._current["id"], first["id"])
        self.assertEqual(self.quiz._current["choices"], first["choices"])

    def test_close_cancels_autostart_without_recording_a_hidden_question(self) -> None:
        """Closing the practice view stops at the next cooperative preparation checkpoint."""

        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        generate = self.generator.generate

        def blocked(rows, direction):
            """Hold an in-flight request until close; e.g. cancellation stays independent of HTTP latency."""

            entered.set()
            release.wait(3)
            return generate(rows, direction)

        self.generator.generate = blocked
        self._open()
        self._wait(entered.is_set)
        task = self.quiz._task
        cancelled = []
        task.signals.cancelled.connect(lambda: cancelled.append(True))
        self.dialog.close()
        self.assertTrue(task.cancel_requested)
        release.set()
        self._wait(lambda: bool(cancelled))
        self.assertIsNone(self.db.practice_active())
        self.assertEqual(practice_stats.snapshot(self.db)["totals"]["shown"], 0)

    def test_empty_vocabulary_is_a_clear_state_without_generation(self) -> None:
        """Guide a first-time learner without showing a failure; e.g. an empty catalog has no request to send."""

        self.db = library_db.LibraryDb(self.db.path.with_name("empty.sqlite3"))
        self.db.initialize()
        self.engine = practice_service.PracticeService(self.db, self.generator)
        self._open()
        self._wait(lambda: self.quiz._task is None)
        self.assertEqual(self.quiz._prompt.text(), "No words ready yet")
        self.assertEqual(self.generator.calls, [])
        self.assertIsNone(self.quiz._current)

    def test_round_options_accept_typing_without_answering_or_continuing(self) -> None:
        """Preserve native editing during the exercise; e.g. enter twelve terms while a question is on screen."""

        self._open()
        self._ready()
        row = self.quiz._current
        menu = self.quiz._controls.menu
        menu.popup(QtCore.QPoint(30, 30))
        editor = self.quiz._controls.count.lineEdit()
        editor.setFocus()
        editor.selectAll()
        QtTest.QTest.keyClicks(editor, "12")
        QtTest.QTest.keyClick(editor, QtCore.Qt.Key.Key_Return)
        self.assertEqual(self.quiz._controls.count.value(), 12)
        self.assertEqual(self.quiz._current["id"], row["id"])
        self.assertIsNone(self.quiz._current["outcome"])
        menu.hide()


if __name__ == "__main__":
    unittest.main()
