"""Verify the native typed-answer loop, asynchronous recovery and shared activity.

Example: `python -m unittest tests.test_library_verbs` sends real Qt events without remote API calls.
"""

import os
import json
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

from tests import listening_fixtures
from tests import practice_fixtures
from tests import verb_fixtures
from yt_whisper_subs import library_db
from yt_whisper_subs import library_listening
from yt_whisper_subs import library_practice
from yt_whisper_subs import library_pronunciation
from yt_whisper_subs import library_theme
from yt_whisper_subs import listening_tts
from yt_whisper_subs import practice_activity
from yt_whisper_subs import practice_service
from yt_whisper_subs import pronunciation
from yt_whisper_subs import verb_service
from yt_whisper_subs import verb_stats


class VerbFlowTests(unittest.TestCase):
    """Exercise actual native input transitions; e.g. correction is a separate action from continuation."""

    @classmethod
    def setUpClass(cls) -> None:
        """Share native Qt dispatch; e.g. Enter behavior matches the real dialog."""

        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self) -> None:
        """Isolate catalogs, providers and windows; e.g. no user history or API credits are touched."""

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = library_db.LibraryDb(Path(tmp.name) / "catalog.sqlite3")
        self.db.initialize()
        catalog = verb_fixtures.catalog(self.db)
        patch = mock.patch.object(self.db, "verb_catalog", return_value=catalog)
        patch.start()
        self.addCleanup(patch.stop)
        self.provider = verb_fixtures.FakeVerbProvider()
        self.engine = verb_service.VerbService(self.db, self.provider)
        self.db.set_setting("verb_options", verb_fixtures.options().model_dump_json())
        self.dialog = None
        self.addCleanup(self._close)

    def _close(self) -> None:
        """Drain workers before deleting the catalog; e.g. cancelled requests cannot outlive temporary files."""

        if self.dialog is not None and shiboken6.isValid(self.dialog):
            self.dialog.close()
        QtCore.QThreadPool.globalInstance().waitForDone(5000)
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def _open(self, *, activity=False) -> None:
        """Inject both providers before showing; e.g. automatic question preparation cannot reach the network."""

        vocab = practice_service.PracticeService(self.db, practice_fixtures.FakeGenerator())
        self.dialog = library_practice.PracticeDialog(SimpleNamespace(db=self.db), engine=vocab)
        self.panel = self.dialog._verbs
        self.panel._engine = self.engine
        self.dialog.setStyleSheet(library_theme.STYLE + library_practice._STYLE)
        self.dialog.setAttribute(QtCore.Qt.WidgetAttribute.WA_DontShowOnScreen)
        self.dialog.show_page(True)
        if not activity:
            self.dialog._tabs.setCurrentIndex(library_practice.Page.VERBS)
        self.dialog.show()
        self.app.processEvents()

    def _wait(self, predicate) -> None:
        """Wait for observable state while dispatching Qt; e.g. a correction can finish in a background thread."""

        deadline = time.monotonic() + 5
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.01)
        self.assertTrue(predicate())

    def _answer(self, *, wrong=False) -> None:
        """Type a real sentence and press Enter; e.g. blank input never starts a grader."""

        row = self.panel._run.row
        answer = row["content"]["answer"]
        if wrong:
            answer = answer.replace("werkt", "werk").replace("maakt", "maak")
        self.panel._view.card.editor.setPlainText(answer)
        QtTest.QTest.keyClick(self.panel._view.card.editor, QtCore.Qt.Key.Key_Return)

    def test_verb_answer_speech_is_ready_before_the_question_appears(self) -> None:
        """Reveal a correction from cache even when grading displays the learner's variant."""

        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        class Piper(listening_fixtures.Speech):
            """Use the real Piper scheduling lane with deterministic offline waveforms."""

            def render_many(self, jobs, report) -> None:
                """Render fixture audio under the same lane as the native backend."""

                with listening_tts.synthesis("piper"):
                    entered.set()
                    if not release.wait(5):
                        raise TimeoutError("Test did not release the speech job")
                    super().render_many(jobs, report)

        self.db.set_setting(library_pronunciation.SETTING, 1)
        speaker = Piper()
        audio = pronunciation.Audio(self.db, speech=speaker)
        with (listening_tts.cache_writes("test-unrelated-voice"), listening_tts.synthesis("zonos"),
              mock.patch.object(library_listening, "WavePlayer", listening_fixtures.Player),
              mock.patch.object(pronunciation, "Audio", return_value=audio)):
            self._open()
            controls = self.dialog._pronunciation
            self._wait(entered.is_set)
            self.assertIsNone(self.panel._run.row)
            self.assertEqual(self.db.verb_summary(self.panel._run.session)["shown"], 0)
            release.set()
            self._wait(lambda: self.panel._run.row is not None)
            row = self.panel._run.row
            source, answer = (row["content"][key] for key in ("source", "answer"))
            source_clip, answer_clip = audio.cached([source, answer])
            self._wait(lambda: controls._player.calls == [source_clip])
            calls = len(speaker.calls)
            speaker.available = False
            variant = answer.lower().rstrip(".")
            self.panel._view.card.editor.setPlainText(variant)
            self.panel._view.action.click()
            self._wait(lambda: controls._player.calls == [source_clip, answer_clip])
            self.assertEqual(self.panel._run.row["grade"]["corrected"], variant)
            self.assertEqual(len(speaker.calls), calls)
            self.assertFalse(controls._status.text())

    def test_auto_first_question_submit_feedback_and_separate_continue(self) -> None:
        """Require no Start round action and no automatic advancement; e.g. Enter checks, then a fresh gesture continues."""

        self._open()
        self._wait(lambda: self.panel._run.row is not None)
        first = self.panel._run.row["id"]
        self.assertFalse(self.panel._view.action.isEnabled())
        self._answer(wrong=True)
        self._wait(lambda: self.panel._run.row["outcome"] == "wrong")
        self.assertEqual(self.panel._run.row["id"], first)
        self.assertIn("→", self.panel._view.card._details.text())
        self.assertIn(self.panel._run.row["grade"]["corrected"], self.panel._view.card._correction.text())
        self.assertEqual(practice_activity.read(self.db).week[-1].verbs, 1)
        self.panel._view.action.click()
        self.assertEqual(self.panel._run.row["id"], first)
        QtTest.QTest.qWait(QtWidgets.QApplication.doubleClickInterval() + 30)
        QtTest.QTest.keyClick(self.panel._view.action, QtCore.Qt.Key.Key_Return)
        self.assertNotEqual(self.panel._run.row["id"], first)
        self.assertEqual(self.panel._view.card.editor.toPlainText(), "")
        repeat = QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_Return,
                                 QtCore.Qt.KeyboardModifier.NoModifier, "", True, 1)
        self.app.sendEvent(self.panel._view.card.editor, repeat)
        self.assertEqual(practice_activity.read(self.db).answers, 1)

    def test_grade_failure_retry_and_resume_preserve_first_attempt(self) -> None:
        """A network failure is not an incorrect answer; e.g. reopening offers the identical saved sentence to check again."""

        self._open()
        self._wait(lambda: self.panel._run.row is not None)
        self.provider.available = False
        self._answer(wrong=True)
        self._wait(lambda: self.panel._task is None)
        saved = self.panel._run.row
        self.assertTrue(self.panel._view.card.editor.isReadOnly())
        self.assertEqual(self.panel._view.action.text(), "Retry checking")
        self.assertEqual(verb_stats.snapshot(self.db)["wrong"], 0)
        self._close()
        self._open()
        self._wait(lambda: self.panel._run.row is not None)
        self.assertEqual(self.panel._run.row["answer_text"], saved["answer_text"])
        self.provider.available = True
        self.panel._view.action.click()
        self._wait(lambda: self.panel._run.row["outcome"] == "wrong")
        self.assertEqual(practice_activity.read(self.db).answers, 1)

    def test_activity_and_hidden_ready_round_do_not_expose_questions(self) -> None:
        """Keep all statistics pages read-only; e.g. generation can finish after switching to Activity."""

        self._open(activity=True)
        QtTest.QTest.qWait(100)
        self.assertEqual(self.provider.generated, 0)
        self.assertIsNone(self.db.verb_active())
        entered, release = threading.Event(), threading.Event()
        original = self.provider.generate

        def delayed(rows):
            """Hold the provider while switching pages; e.g. reproduce a normal slow network response."""

            entered.set()
            release.wait(5)
            return original(rows)

        self.provider.generate = delayed
        self.dialog._tabs.setCurrentIndex(library_practice.Page.VERBS)
        self._wait(entered.is_set)
        self.dialog._tabs.setCurrentIndex(library_practice.Page.ACTIVITY)
        release.set()
        self._wait(lambda: self.panel._task is None)
        session = self.db.verb_active()
        self.assertEqual(self.db.verb_summary(session)["shown"], 0)
        self.dialog._tabs.setCurrentIndex(library_practice.Page.VERBS)
        self._wait(lambda: self.panel._run.row is not None)
        self.assertEqual(self.db.verb_summary(session)["shown"], 1)

    def test_report_before_answer_skips_grading_and_continues(self) -> None:
        """Reporting remains available before attempting a faulty task; e.g. it never adds activity or a failure."""

        self._open()
        self._wait(lambda: self.panel._run.row is not None)
        self.panel._view.flag.click()
        self.assertEqual(self.panel._run.row["outcome"], "invalid")
        self.assertEqual(practice_activity.read(self.db).answers, 0)
        QtTest.QTest.qWait(QtWidgets.QApplication.doubleClickInterval() + 30)
        self.panel._view.action.click()
        self.assertIsNone(self.panel._run.row["outcome"])
        self.assertEqual(self.provider.graded, 0)

    def test_insights_render_independent_dimensions_and_mistake_history(self) -> None:
        """Show usable evidence rather than only a total score; e.g. agreement errors appear in the native tables."""

        self._open()
        self._wait(lambda: self.panel._run.row is not None)
        self._answer(wrong=True)
        self._wait(lambda: self.panel._run.row["outcome"] == "wrong")
        self.dialog._tabs.setCurrentIndex(library_practice.Page.PROGRESS)
        self.assertEqual(self.dialog._progress_tabs.currentIndex(), 1)
        self.assertEqual(self.dialog._verb_progress._tables["Forms"].rowCount(), 4)
        self.assertEqual(self.dialog._verb_progress._tables["Mistakes"].rowCount(), 1)
        self.assertEqual(len(self.dialog._verb_progress._accuracy._series[0]["points"]), 1)
        self.dialog._tabs.setCurrentIndex(library_practice.Page.ACTIVITY)
        self.assertEqual(self.dialog._activity._data.answers, 1)

    def test_explicit_practice_returns_from_verb_insights_to_the_sentence(self) -> None:
        """Honor the reminder's Practice now action in the last mode; e.g. it opens exercises rather than remaining in analytics."""

        self._open()
        self._wait(lambda: self.panel._run.row is not None)
        first = self.panel._run.row["id"]
        self.dialog._tabs.setCurrentIndex(library_practice.Page.PROGRESS)
        self.dialog._progress_tabs.setCurrentIndex(1)
        self.dialog.show_page(True)
        self.dialog.show_page(False)
        self.assertEqual(self.dialog._tabs.currentIndex(), library_practice.Page.VERBS)
        self.assertEqual(self.panel._run.row["id"], first)

    def test_options_enter_cannot_continue_feedback(self) -> None:
        """Suspend answer actions while editing settings; e.g. entering a round length cannot skip the corrected sentence."""

        self._open()
        self._wait(lambda: self.panel._run.row is not None)
        self._answer(wrong=True)
        self._wait(lambda: self.panel._run.row["outcome"] == "wrong")
        first = self.panel._run.row["id"]
        QtTest.QTest.qWait(QtWidgets.QApplication.doubleClickInterval() + 30)
        menu = self.panel._view.options.menu
        menu.setAttribute(QtCore.Qt.WidgetAttribute.WA_DontShowOnScreen)
        menu.popup(self.dialog.mapToGlobal(QtCore.QPoint(150, 150)))
        self.app.processEvents()
        self.assertTrue(menu.isVisible())
        self.panel._view.options._count.setValue(12)
        QtTest.QTest.keyClick(self.panel._view.options._count, QtCore.Qt.Key.Key_Return)
        self.assertEqual(self.panel._run.row["id"], first)
        self.assertEqual(practice_activity.read(self.db).answers, 1)
        menu.close()

    def test_close_during_grading_keeps_a_pending_attempt(self) -> None:
        """Cancel without waiting for the provider; e.g. a late HTTP result cannot create an unwanted review after closing."""

        self._open()
        self._wait(lambda: self.panel._run.row is not None)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = self.provider.grade

        def delayed(document):
            """Hold correction at its remote boundary; e.g. close immediately while it is in flight."""

            entered.set()
            release.wait(5)
            return original(document)

        self.provider.grade = delayed
        row_id = self.panel._run.row["id"]
        self._answer(wrong=True)
        self._wait(entered.is_set)
        self.dialog.close()
        release.set()
        self.assertTrue(QtCore.QThreadPool.globalInstance().waitForDone(5000))
        saved = self.db.verb_get(row_id)
        self.assertIsNotNone(saved["answer_text"])
        self.assertIsNone(saved["outcome"])
        self.assertEqual(practice_activity.read(self.db).answers, 1)

    def test_source_english_reveal_is_instant_scoped_and_preserves_typing(self) -> None:
        """Use cached English without revealing the transformed answer; e.g. Alt+E never consumes ordinary typed e."""

        self._open()
        self._wait(lambda: self.panel._run.row is not None)
        card = self.panel._view.card
        card.editor.setPlainText("My draft")
        card.editor.setFocus()
        self.dialog.activateWindow()
        self.app.processEvents()
        QtTest.QTest.keyClick(card.editor, QtCore.Qt.Key.Key_E, QtCore.Qt.KeyboardModifier.AltModifier)
        self.assertTrue(card.meaning._button.isChecked())
        self.assertEqual(card.meaning._label.text(), self.panel._run.row["content"]["source_english"])
        self.assertEqual(card.editor.toPlainText(), "My draft")
        self.assertTrue(card.editor.hasFocus())
        self.assertEqual(card._translation.text(), "")
        self.assertEqual(card._correction.text(), "")
        self.assertEqual(self.provider.translated, [])
        self.assertEqual(practice_activity.read(self.db).answers, 0)
        self.dialog._tabs.setCurrentIndex(library_practice.Page.ACTIVITY)
        QtTest.QTest.keyClick(self.dialog, QtCore.Qt.Key.Key_E, QtCore.Qt.KeyboardModifier.AltModifier)
        self.assertTrue(card.meaning._button.isChecked())

    def test_feedback_includes_transformed_english_and_help_never_advances(self) -> None:
        """Translate the corrected form after submission; e.g. showing the original meaning is independent of Continue."""

        self._open()
        self._wait(lambda: self.panel._run.row is not None)
        first = self.panel._run.row["id"]
        self._answer(wrong=True)
        self._wait(lambda: self.panel._run.row["outcome"] == "wrong")
        card = self.panel._view.card
        self.assertEqual(card._translation.text(), self.panel._run.row["grade"]["corrected_english"])
        QtTest.QTest.qWait(QtWidgets.QApplication.doubleClickInterval() + 30)
        card.meaning._button.click()
        card.meaning._button.click()
        self.assertEqual(self.panel._run.row["id"], first)
        self.panel._view.action.click()
        self.assertNotEqual(self.panel._run.row["id"], first)
        self.assertFalse(card.meaning._button.isChecked())
        self.assertEqual(card._translation.text(), "")

    def test_existing_round_can_retry_source_translation_without_grading(self) -> None:
        """Keep old immutable questions usable; e.g. a failed on-demand source translation leaves draft text intact."""

        session = self.engine.start(verb_fixtures.options(1), lambda _: None)
        row = self.db.verb_current(session)
        content = row["content"].copy()
        content.pop("source_english")
        with self.db._connect() as conn:
            conn.execute("UPDATE verb_questions SET body_json=? WHERE id=?", (json.dumps(content), row["question_id"]))
        self._open()
        self._wait(lambda: self.panel._run.row is not None)
        card = self.panel._view.card
        card.editor.setPlainText("My unfinished sentence")
        self.provider.available = False
        card.meaning._button.click()
        self._wait(lambda: not self.panel._help_tasks)
        self.assertIn("Retry English", card.meaning._button.text())
        self.assertEqual(card.editor.toPlainText(), "My unfinished sentence")
        self.assertEqual(practice_activity.read(self.db).answers, 0)
        self.provider.available = True
        card.meaning._button.click()
        self._wait(lambda: not self.panel._help_tasks)
        self.assertEqual(card.meaning._label.text(), self.engine.english(row["question_id"], lambda _: None))
        self.assertEqual(self.provider.translated, [content["source"], content["source"]])

    def test_main_progress_offers_verbs_without_starting_an_exercise(self) -> None:
        """Expose verb statistics at the normal Progress entry point; e.g. inspecting empty verb history costs no API request."""

        self._open(activity=True)
        self.dialog._tabs.setCurrentIndex(library_practice.Page.PROGRESS)
        self.dialog._progress_tabs.setCurrentIndex(1)
        self.assertEqual(self.dialog._progress_tabs.tabText(1), "Verbs")
        self.assertIn("0 submitted", self.dialog._verb_progress._totals.text())
        self.assertEqual(self.provider.generated, 0)
        self.assertIsNone(self.db.verb_active())

    def test_late_source_english_cannot_overwrite_the_next_question(self) -> None:
        """Tag and cancel background help at question boundaries; e.g. a slow translation is never shown beneath another sentence."""

        session = self.engine.start(verb_fixtures.options(), lambda _: None)
        row = self.db.verb_current(session)
        content = row["content"].copy()
        content.pop("source_english")
        with self.db._connect() as conn:
            conn.execute("UPDATE verb_questions SET body_json=? WHERE id=?", (json.dumps(content), row["question_id"]))
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def slow(_sentence):
            """Hold one source translation; e.g. the learner submits and continues before it completes."""

            entered.set()
            release.wait(5)
            return "Old sentence meaning"

        self.provider.translate_sentence = slow
        self._open()
        self._wait(lambda: self.panel._run.row is not None)
        self.panel._view.card.meaning._button.click()
        self._wait(entered.is_set)
        first = self.panel._run.row["id"]
        self._answer()
        self._wait(lambda: self.panel._run.row["outcome"] == "correct")
        QtTest.QTest.qWait(QtWidgets.QApplication.doubleClickInterval() + 30)
        self.panel._view.action.click()
        self.assertNotEqual(self.panel._run.row["id"], first)
        release.set()
        self.assertTrue(QtCore.QThreadPool.globalInstance().waitForDone(5000))
        self.app.processEvents()
        self.assertFalse(self.panel._view.card.meaning._button.isChecked())
        self.assertNotEqual(self.panel._view.card.meaning._label.text(), "Old sentence meaning")


if __name__ == "__main__":
    unittest.main()
