"""Exercise the real native sentence page and its shared learning statistics.

Example: `python -m unittest tests.test_library_sentences` needs neither personal history nor API credits.
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
from PySide6 import QtTest
from PySide6 import QtWidgets
import shiboken6

from tests import listening_fixtures
from tests import practice_fixtures
from tests import sentence_fixtures as fixtures
from yt_whisper_subs import library_db
from yt_whisper_subs import library_listening
from yt_whisper_subs import library_practice
from yt_whisper_subs import library_pronunciation
from yt_whisper_subs import library_theme
from yt_whisper_subs import practice
from yt_whisper_subs import practice_activity
from yt_whisper_subs import practice_service
from yt_whisper_subs import pronunciation
from yt_whisper_subs import sentence_service
from yt_whisper_subs import sentence_stats


class SentenceFlowTests(unittest.TestCase):
    """Drive native events against actual SQLite; e.g. viewing progress never starts a paid exercise."""

    @classmethod
    def setUpClass(cls) -> None:
        """Share one native event loop; e.g. Enter follows the installed Qt behavior."""

        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        # Mirror the tray app: closing practice must not quit its application-owned correction queue.
        cls.app.setQuitOnLastWindowClosed(False)

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
        self.dialog = library_practice.PracticeView(SimpleNamespace(db=self.db), engine=vocab)
        self.dialog.resize(1160, 900)
        self.dialog.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        opts = fixtures.options().model_validate_json(self.db.setting("sentence_options", fixtures.options().model_dump_json()))
        self.panel = self.dialog._sentences[opts.direction]
        self.panel._engine = self.engine
        self.dialog.setStyleSheet(library_theme.STYLE + library_practice._STYLE)
        self.dialog.setAttribute(QtCore.Qt.WidgetAttribute.WA_DontShowOnScreen)
        self.dialog.show_page(True)
        if not activity:
            self.dialog._tabs.setCurrentIndex(library_practice.Page.SENTENCE_EN_NL if opts.direction == practice.Direction.EN_NL else library_practice.Page.SENTENCE_NL_EN)
        self.dialog.show()
        self.app.setActiveWindow(self.dialog)
        self.app.processEvents()

    def _wait(self, predicate) -> None:
        """Wait for observable asynchronous state; e.g. a native correction arrives from a background worker."""

        deadline = time.monotonic() + 10
        while not predicate() and time.monotonic() < deadline:
            QtTest.QTest.qWait(10)
        if not predicate():
            session = self.panel._run.session
            history = self.db.sentence_history(session) if session else []
            self.fail(f"Sentence state did not arrive: {history}")

    def _answer(self, text: str) -> None:
        """Submit using the real editor event; e.g. one Enter creates one immutable attempt."""

        self.panel._view.card.editor.setPlainText(text)
        QtTest.QTest.keyClick(self.panel._view.card.editor, QtCore.Qt.Key.Key_Return)

    def test_dutch_translation_review_uses_prepared_model_audio(self) -> None:
        """Keep the English prompt silent and play the saved Dutch target when feedback opens."""

        self.db.set_setting(library_pronunciation.SETTING, 1)
        voice = listening_fixtures.Speech()
        audio = pronunciation.Audio(self.db, speech=voice)
        with (mock.patch.object(pronunciation, "Audio", return_value=audio),
              mock.patch.object(library_listening, "WavePlayer", listening_fixtures.Player)):
            self._open()
            self._wait(lambda: self.panel._run.row is not None)
            first = self.panel._run.row
            controls = self.dialog._pronunciation
            approved = first["content"]["dutch"]
            card = self.panel._view.card
            self.assertFalse(card._audio_heading.isVisible())
            self.assertFalse(card._personal_correction.isVisible())
            self.assertFalse(controls._listen.isEnabled())
            self.assertFalse(controls._player.calls)
            cached = audio.cached([approved])
            self.assertTrue(cached)
            calls = len(voice.calls)
            voice.available = False
            variant = approved.upper()
            self._answer(variant)
            self._wait(lambda: self.db.sentence_get(first["id"])["outcome"] == "correct")
            self.panel._jump(first["id"])
            self._wait(lambda: bool(controls._player.calls))
            self.assertEqual(controls._player.calls, cached)
            self.assertEqual(card._correction.text(), approved)
            self.assertEqual(controls._listen.toolTip(), card._correction.text())
            self.assertTrue(card._audio_heading.isVisible())
            self.assertIn(variant, card._personal_correction.text())
            self.assertEqual(self.panel._run.row["grade"]["corrected"], variant)
            self.assertEqual(len(voice.calls), calls)

    def test_submit_advances_and_history_restores_highlighted_feedback(self) -> None:
        """Start source-only and continue immediately; e.g. the previous answer later exposes its actual superlative mistake."""

        self._open()
        self._wait(lambda: self.panel._run.row is not None)
        card, first = self.panel._view.card, self.panel._run.row["id"]
        self.assertEqual(card._source.text(), fixtures.ENGLISH)
        self.assertFalse(card.meaning.isVisible())
        self.assertEqual((card._correction.text(), card._translation.text(), card._targets.text()), ("", "", ""))
        self._answer(fixtures.DUTCH.replace("slimste", "slimmere"))
        self.assertNotEqual(self.panel._run.row["id"], first)
        second = self.panel._run.row["id"]
        card.editor.setPlainText("Mijn onvoltooide vertaling")
        self._wait(lambda: self.db.sentence_get(first)["outcome"] == "wrong")
        self.assertEqual(card.editor.toPlainText(), "Mijn onvoltooide vertaling")
        rail = self.panel._rail
        QtTest.QTest.mouseClick(rail.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=rail.visualItemRect(rail.item(0)).center())
        self.assertEqual(self.panel._run.row["id"], first)
        self.assertIn("superlative", card._details.text())
        self.assertIn("Partial score: 89%", card._verdict.text())
        self.assertEqual(card._correction.text(), fixtures.DUTCH)
        self.assertFalse(card._audio_heading.isVisible())
        self.assertFalse(card._personal_correction.isVisible())
        self.assertEqual(card._translation.text(), fixtures.ENGLISH)
        self.assertIn("slimste", card._targets.text())
        self.assertEqual([item.cursor.selectedText() for item in card.editor.extraSelections()], ["slimmere"])
        self.panel._view.action.click()
        self.assertEqual(self.panel._run.row["id"], second)
        self.assertEqual(card.editor.toPlainText(), "Mijn onvoltooide vertaling")
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
            presentation_id = self.panel._run.row["id"]
            self._answer(answer)
            self._wait(lambda: self.db.sentence_get(presentation_id)["outcome"] == "correct")
            self.dialog._tabs.setCurrentIndex(library_practice.Page.PROGRESS)
            self.assertEqual(self.dialog._progress_tabs.currentIndex(), 2)
            self.assertIn("submitted", self.dialog._sentence_progress._totals.text())
            self.assertIn("100% partial score", self.dialog._sentence_progress._totals.text())
            self.assertTrue(any(series["points"] for series in self.dialog._sentence_progress._partial._series))
            self.assertGreater(self.dialog._sentence_progress._tabs.tables["Sentences"].rowCount(), 0)
            self.assertGreater(self.dialog._sentence_progress._tabs.tables["Forms"].rowCount(), 0)
            self._close()
        data = practice_activity.read(self.db)
        self.assertEqual((data.answers, data.week[-1].sentences, data.distinct_words), (2, 2, 3))
        self.assertEqual((data.week[-1].sentence_en_nl, data.week[-1].sentence_nl_en), (1, 1))

    def test_statistics_only_never_generate_or_expose(self) -> None:
        """Keep analytics read-only; e.g. inspecting sentence forms does not automatically start a round."""

        self._open(activity=True)
        self.dialog._tabs.setCurrentIndex(library_practice.Page.PROGRESS)
        self.dialog._progress_tabs.setCurrentIndex(2)
        QtTest.QTest.qWait(100)
        self.assertEqual((self.provider.calls, self.provider.graded), ([], []))
        self.assertIsNone(self.db.sentence_active())
        self.assertEqual(sentence_stats.snapshot(self.db)["shown"], 0)

    def test_top_level_direction_switch_preserves_both_drafts(self) -> None:
        """Keep reading and writing drafts independent; e.g. switching directions never stops the sentence the learner was composing."""

        self._open()
        self._wait(lambda: self.panel._run.row is not None)
        first = self.panel
        first._view.card.editor.setPlainText("Mijn eerste concept")
        second = self.dialog._sentences[practice.Direction.NL_EN]
        second._engine = self.engine
        self.dialog._nav._buttons.button(library_practice.Page.SENTENCE_NL_EN).click()
        self._wait(lambda: second._run.row is not None)
        second._view.card.editor.setPlainText("My other draft")
        self.dialog._nav._buttons.button(library_practice.Page.SENTENCE_EN_NL).click()
        self.assertEqual(first._view.card.editor.toPlainText(), "Mijn eerste concept")
        self.assertEqual(self.db.sentence_draft(second._run.row["id"])["text"], "My other draft")
        self.assertEqual(self.db.sentence_active("en_nl"), first._run.session)
        self.assertEqual(self.db.sentence_active("nl_en"), second._run.session)

    def test_pending_correction_resumes_original_answer(self) -> None:
        """Do not turn network errors into learning failures; e.g. reopening retries the saved first attempt exactly once."""

        self._open()
        self._wait(lambda: self.panel._run.row is not None)
        first = self.panel._run.row["id"]
        self.provider.available = False
        text = fixtures.DUTCH.replace("slimste", "slimmere")
        self._answer(text)
        self._wait(lambda: self.db.sentence_history(self.panel._run.session)[0]["state"] == "failed")
        self.panel._jump(first)
        self.assertEqual(self.panel._view.action.text(), "Retry correction")
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

    def test_slow_checks_queue_while_typing_and_survive_dialog_close(self) -> None:
        """Keep one paid request in flight while later answers queue; e.g. closing practice does not cancel a submitted correction."""

        opts = fixtures.options().model_copy(update={"count": 3})
        self.db.set_setting("sentence_options", opts.model_dump_json())
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = self.provider.grade

        def slow(document):
            """Hold a real grading operation at the network boundary; e.g. the GUI must keep accepting later sentences."""

            entered.set()
            if not release.wait(20):
                raise TimeoutError("Test did not release the delayed correction")
            return original(document)

        self.provider.grade = slow
        self._open()
        self._wait(lambda: self.panel._run.row is not None)
        session = self.panel._run.session
        first = self.panel._run.row["id"]
        self._answer(fixtures.DUTCH.replace("slimste", "slimmere"))
        self._wait(entered.is_set)
        self.assertNotEqual(self.panel._run.row["id"], first)
        QtTest.QTest.qWait(QtWidgets.QApplication.doubleClickInterval() + 20)
        self._answer(fixtures.DUTCH)
        self.assertEqual([row["state"] for row in self.db.sentence_history(session)][:2], ["checking", "queued"])
        self.panel._view.card.editor.setPlainText("Een bewaard concept")
        self.dialog.close()
        self.app.processEvents()
        release.set()
        self._wait(lambda: self.db.sentence_summary(session)["correct"] == 1)
        self._open()
        self._wait(lambda: self.panel._run.row is not None)
        self.assertEqual(self.panel._view.card.editor.toPlainText(), "Een bewaard concept")
        self.assertEqual(self.db.sentence_summary(session)["wrong"], 1)
        self.assertEqual(practice_activity.read(self.db).answers, 2)
        self.assertIn("✕", self.panel._rail.item(0).text())
        self.assertIn("✓", self.panel._rail.item(1).text())

    def test_report_pending_answer_wins_over_late_remote_result(self) -> None:
        """Quarantine safely while correction is in flight; e.g. its remote answer cannot restore an invalid exercise or duplicate activity."""

        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = self.provider.grade

        def slow(document):
            """Delay only the remote judgment; e.g. reporting happens before the grade transaction."""

            entered.set()
            release.wait(20)
            return original(document)

        self.provider.grade = slow
        self._open()
        self._wait(lambda: self.panel._run.row is not None)
        first = self.panel._run.row["id"]
        self._answer(fixtures.DUTCH.replace("slimste", "slimmere"))
        self._wait(entered.is_set)
        self.panel._jump(first)
        self.panel._view.flag.click()
        release.set()
        self._wait(lambda: self.panel._checks._task is None)
        self.assertEqual(self.db.sentence_get(first)["outcome"], "invalid")
        self.assertEqual(sentence_stats.snapshot(self.db)["wrong"], 0)
        self.assertEqual(practice_activity.read(self.db).answers, 1)

    def test_new_round_keeps_previous_corrections_and_new_draft_independent(self) -> None:
        """Correct previously submitted work after round replacement; e.g. its delayed response cannot overwrite the next round's editor."""

        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = self.provider.grade

        def slow(document):
            """Hold only correction while preparation remains available; e.g. a new round can be built concurrently."""

            entered.set()
            release.wait(20)
            return original(document)

        self.provider.grade = slow
        self._open()
        self._wait(lambda: self.panel._run.row is not None)
        previous = self.panel._run.session
        self._answer(fixtures.DUTCH.replace("slimste", "slimmere"))
        self._wait(entered.is_set)
        self.panel._prepare(cached_first=True)
        self._wait(lambda: self.panel._run.session != previous and self.panel._run.row is not None)
        current = self.panel._run.row["id"]
        self.panel._view.card.editor.setPlainText("Mijn nieuwe concept")
        release.set()
        self._wait(lambda: self.db.sentence_summary(previous)["wrong"] == 1)
        self.assertEqual(self.panel._run.row["id"], current)
        self.assertEqual(self.panel._view.card.editor.toPlainText(), "Mijn nieuwe concept")
        self.assertEqual(practice_activity.read(self.db).answers, 1)

    def test_final_submission_opens_unseen_feedback_and_page_clicks_walk_all_answers(self) -> None:
        """Review automatically after writing and continue in session order; e.g. a later already-viewed answer still opens before the end summary."""

        self._open()
        self._wait(lambda: self.panel._run.row is not None)
        reviewed = []
        self.panel.round_reviewed.connect(lambda: reviewed.append(True))
        first = self.panel._run.row["id"]
        self._answer(fixtures.DUTCH.replace("slimste", "slimmere"))
        self._wait(lambda: self.db.sentence_get(first)["outcome"] == "wrong")
        self.assertIsNone(self.db.sentence_get(first)["viewed_at"])
        self._wait(lambda: "•" in self.panel._rail.item(0).text())
        second = self.panel._run.row["id"]
        QtTest.QTest.qWait(QtWidgets.QApplication.doubleClickInterval() + 20)
        self._answer(fixtures.DUTCH)
        self.assertEqual(self.panel._run.row["id"], first)
        self.assertIsNotNone(self.db.sentence_get(first)["viewed_at"])
        self.assertIn("cleverest", self.panel._view.card._source.text())
        self.assertIn('href="error:', self.panel._view.card._source.text())
        self._wait(lambda: self.db.sentence_get(second)["outcome"] == "correct")
        self.assertIsNone(self.db.sentence_get(second)["viewed_at"])
        self.assertFalse(reviewed)
        for expected in (second, None):
            QtTest.QTest.qWait(QtWidgets.QApplication.doubleClickInterval() + 20)
            card = self.panel._view.card
            QtTest.QTest.mouseClick(card, QtCore.Qt.MouseButton.LeftButton, pos=QtCore.QPoint(card.width() - 8, card.height() - 8))
            self.assertEqual(self.panel._run.row["id"] if self.panel._run.row else None, expected)
        self.assertEqual(self.panel._view.card._source.text(), "Round reviewed")
        self.assertEqual(reviewed, [True])
        self.assertIn("1 correct", self.panel._view.card._instruction.text())
        self.assertIn("1 to revisit", self.panel._view.card._instruction.text())
        self.assertNotIn("•", self.panel._rail.item(1).text())
        viewed = self.db.sentence_get(first)["viewed_at"]
        self.panel._jump(first)
        QtTest.QTest.qWait(QtWidgets.QApplication.doubleClickInterval() + 20)
        self.panel._view.card._source.linkActivated.emit("error:0")
        self.assertEqual(self.panel._run.row["id"], second)
        self.assertEqual(self.db.sentence_get(first)["viewed_at"], viewed)
        self.assertEqual(practice_activity.read(self.db).answers, 2)

    def test_hidden_feedback_stays_unseen_and_opens_when_returning(self) -> None:
        """Wait meaningfully while grading and never mark hidden feedback viewed; e.g. Progress cannot consume an unread correction."""

        opts = fixtures.options()
        self.db.set_setting("sentence_options", opts.model_dump_json())
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = self.provider.grade

        def slow(document):
            """Let the learner leave before the response arrives; e.g. a hidden result needs to remain unread."""

            entered.set()
            release.wait(20)
            return original(document)

        self.provider.grade = slow
        self._open()
        self._wait(lambda: self.panel._run.row is not None)
        first = self.panel._run.row["id"]
        self._answer(fixtures.DUTCH.replace("slimste", "slimmere"))
        self._wait(entered.is_set)
        self.assertEqual(self.panel._view.card._source.text(), "Waiting for corrections")
        self.dialog._tabs.setCurrentIndex(library_practice.Page.PROGRESS)
        release.set()
        self._wait(lambda: self.db.sentence_get(first)["outcome"] == "wrong")
        self.assertIsNone(self.db.sentence_get(first)["viewed_at"])
        self.dialog._tabs.setCurrentIndex(library_practice.Page.SENTENCE_EN_NL)
        self._wait(lambda: self.panel._run.row is not None)
        self.assertEqual(self.panel._run.row["id"], first)
        self.assertIsNotNone(self.db.sentence_get(first)["viewed_at"])

    def test_reopen_reviews_unseen_feedback_before_unused_prepared_stock(self) -> None:
        """Resume the review backlog after restart without wasting new stock; e.g. a completed round's unseen feedback outranks a never-opened prepared round."""

        opts = fixtures.options().model_copy(update={"include_early": True})
        self.db.set_setting("sentence_options", opts.model_dump_json())
        previous = self.engine.start(opts, lambda _: None)
        row = self.db.sentence_current(previous)
        self.db.sentence_submit(row["id"], fixtures.DUTCH, 1000)
        self.engine.check(row["id"], lambda _: None)
        following = self.engine.start(opts, lambda _: None)
        generations = len(self.provider.calls)
        self._open()
        self._wait(lambda: self.panel._run.row is not None)
        self.assertEqual(self.panel._run.session, previous)
        self.assertEqual(self.panel._run.row["id"], row["id"])
        self.assertIsNotNone(self.db.sentence_get(row["id"])["viewed_at"])
        self.assertEqual(self.db.sentence_summary(following)["shown"], 0)
        self.assertEqual(len(self.provider.calls), generations)

    def test_explicit_new_round_takes_priority_over_old_unseen_feedback(self) -> None:
        """Respect a deliberate New round action; e.g. automatic review restoration must not return to the round just replaced."""

        opts = fixtures.options().model_copy(update={"count": 2, "include_early": True})
        self.db.set_setting("sentence_options", opts.model_dump_json())
        self._open()
        self._wait(lambda: self.panel._run.row is not None)
        previous = self.panel._run.session
        first = self.panel._run.row["id"]
        self._answer(fixtures.DUTCH.replace("slimste", "slimmere"))
        self._wait(lambda: self.db.sentence_get(first)["outcome"] == "wrong")
        self.panel._prepare(cached_first=True)
        self._wait(lambda: self.panel._run.session != previous and self.panel._run.row is not None)
        self.assertEqual(self.panel._run.session, self.db.sentence_active())
        self.assertIsNone(self.db.sentence_get(first)["viewed_at"])

    def test_feedback_in_unfocused_window_stays_unseen_until_focus_returns(self) -> None:
        """Do not confuse background rendering with consultation; e.g. a correction arriving while another app is active stays unread."""

        self.db.set_setting("sentence_options", fixtures.options().model_dump_json())
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = self.provider.grade

        def slow(document):
            """Let the window lose focus before feedback arrives; e.g. drawing an off-focus correction cannot count as reading it."""

            entered.set()
            release.wait(20)
            return original(document)

        self.provider.grade = slow
        self._open()
        self._wait(lambda: self.panel._run.row is not None)
        first = self.panel._run.row["id"]
        self._answer(fixtures.DUTCH.replace("slimste", "slimmere"))
        self._wait(entered.is_set)
        self.app.setActiveWindow(None)
        release.set()
        self._wait(lambda: self.db.sentence_get(first)["outcome"] == "wrong")
        self.assertIsNone(self.db.sentence_get(first)["viewed_at"])
        self.app.setActiveWindow(self.dialog)
        self._wait(lambda: self.db.sentence_get(first)["viewed_at"] is not None)
