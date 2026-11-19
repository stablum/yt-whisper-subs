"""Drive listening from the top-level native destination without real audio or APIs.

Example: the hidden sentence is absent from labels until a durable transcription receives feedback.
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

from tests import listening_fixtures as audio
from tests import sentence_fixtures as fixtures
from yt_whisper_subs import library_db
from yt_whisper_subs import library_listening
from yt_whisper_subs import library_practice
from yt_whisper_subs import library_checks
from yt_whisper_subs import listening_voices
from yt_whisper_subs import practice_activity
from yt_whisper_subs import practice_tracks
from yt_whisper_subs import sentence_practice as sentences
from yt_whisper_subs import sentence_service


class ListeningFlowTests(unittest.TestCase):
    """Exercise production navigation, durable queues and concealed text; e.g. submitting never waits for a translation API."""

    @classmethod
    def setUpClass(cls) -> None:
        """Share the real Qt event loop; e.g. playback callbacks use normal queued events."""

        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self) -> None:
        """Open an already prepared fixture round; e.g. readiness really includes both speech recordings."""

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = library_db.LibraryDb(Path(tmp.name) / "catalog.sqlite3")
        self.db.initialize()
        fixtures.seed(self.db)
        self.engine = sentence_service.SentenceService(self.db, fixtures.FakeSentenceProvider(), speech=audio.Speech())
        opts = fixtures.options(direction=sentences.Direction.LISTEN)
        self.db.set_setting(practice_tracks.Track.LISTENING.settings_key, opts.model_dump_json())
        self.engine.start(opts, lambda _: None, background=True)
        with mock.patch.object(library_listening, "WavePlayer", audio.Player):
            self.dialog = library_practice.PracticeDialog(SimpleNamespace(db=self.db))
        self.addCleanup(self._close)
        self.panel = self.dialog._listening
        self.panel._engine = self.engine
        self.dialog.show_page(True)
        self.dialog.setAttribute(QtCore.Qt.WidgetAttribute.WA_DontShowOnScreen)
        self.dialog.show()
        self.dialog._nav._buttons.button(library_practice.Page.LISTENING).click()
        self._wait(lambda: self.panel._run.row is not None)

    def _close(self) -> None:
        """Drain owned callbacks before deleting fixture history; e.g. a correction cannot outlive its temporary catalog."""

        if shiboken6.isValid(self.dialog):
            self.dialog.close()
        QtCore.QThreadPool.globalInstance().waitForDone(5000)
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def _wait(self, predicate) -> None:
        """Wait for a concrete native state with a short deadline; e.g. failures cannot spend the OpenAI timeout."""

        end = time.monotonic() + 6
        while not predicate() and time.monotonic() < end:
            self.app.processEvents()
            time.sleep(.01)
        self.assertTrue(predicate())

    def test_header_voice_applies_to_dictation_and_read_aloud(self) -> None:
        """Change the common preset from a real practice page and reuse its audio across players."""

        card = self.panel._view.card
        self._wait(lambda: bool(card._player.calls))
        card.editor.setPlainText("Mijn concept")
        selection = listening_voices.read(self.db)
        preset = listening_voices.Preset(id="second", name="Second voice", config=listening_voices.Config(pace=.8))
        selection.presets.append(preset)
        self.db.set_setting(listening_voices.SETTING, selection.model_dump_json())
        bar = self.dialog._voice_bar
        bar._load()
        self.assertTrue(bar.isVisible())
        self.assertTrue(bar._configure.isVisible())

        second = audio.Speech()
        second.profile = "second-practice-voice"
        self.engine.audio._speech = None
        play_count = len(card._player.calls)
        with mock.patch.object(listening_voices, "speech", return_value=second) as synth:
            bar._pick.setCurrentIndex(bar._pick.findData(preset.id))
            bar._pick.activated.emit(bar._pick.currentIndex())
            self._wait(lambda: len(card._player.calls) > play_count)
            self.assertEqual(card.editor.toPlainText(), "Mijn concept")
            self.assertEqual(listening_voices.read(self.db).selected.id, preset.id)
            self.dialog._pronunciation._audio.prepare(["Goedemorgen"], lambda _: None)
            self.assertEqual(synth.call_args.args[1], preset.config)
            self.assertTrue(any(text == "Goedemorgen" for text, _ in second.calls))

    def test_script_is_concealed_then_feedback_reveals_dutch_and_english(self) -> None:
        """Verify real labels, assistance, correction and separate analytics; e.g. source text cannot leak through a hidden help control."""

        card = self.panel._view.card
        self._wait(lambda: bool(card._player.calls))
        for label in card.findChildren(QtWidgets.QLabel):
            self.assertNotIn(fixtures.DUTCH, label.text())
            self.assertNotIn(fixtures.ENGLISH, label.text())
        self.assertFalse(card.meaning.isVisible())
        action = self.panel._view.action
        self.assertTrue(self.dialog.rect().contains(action.mapTo(self.dialog, action.rect().bottomRight())))
        card._slow.click()
        self.assertEqual(practice_activity.read(self.db).answers, 0)
        card.editor.setPlainText(fixtures.DUTCH.replace("slimste", "slimmere"))
        first = self.panel._run.row["id"]
        QtTest.QTest.keyClick(card.editor, QtCore.Qt.Key.Key_Return)
        self._wait(lambda: self.db.sentence_get(first)["outcome"] == "wrong")
        self.panel._jump(first)
        self.assertEqual(card._correction.text(), fixtures.DUTCH)
        self.assertEqual(card._translation.text(), fixtures.ENGLISH)
        card._details.setText("Long correction details. " * 300)
        self.app.processEvents()
        self.assertTrue(self.dialog.rect().contains(action.mapTo(self.dialog, action.rect().bottomRight())))
        self.assertTrue(card.editor._marks)
        self.assertEqual(len(card._player.calls), 2)
        self.dialog._nav._buttons.button(library_practice.Page.PROGRESS).click()
        self.assertEqual(self.dialog._progress_tabs.currentIndex(), 3)
        self.assertIn("1 submitted", self.dialog._listening_progress._totals.text())
        self.assertIn("0 submitted", self.dialog._sentence_progress._totals.text() or "0 submitted")
        self.assertEqual(practice_activity.read(self.db).week[-1].listening, 1)

    def test_missing_audio_repairs_in_background_without_losing_the_draft(self) -> None:
        """Recover an active question after cache deletion; e.g. restoring speech never generates a replacement sentence."""

        card = self.panel._view.card
        self._wait(lambda: bool(card._player.calls))
        card.editor.setPlainText("De slimste")
        row = self.panel._run.row
        cached = self.db.listening_audio(row["question_id"])
        self.db.listening_paths(cached["cache_key"])["normal"].unlink()
        before = len(card._player.calls)
        with mock.patch.object(card, "_cache", return_value=self.engine.audio), \
             mock.patch.object(self.engine.audio, "prepare", wraps=self.engine.audio.prepare) as prepare:
            card._replay.click()
            self._wait(lambda: len(card._player.calls) > before)
            self.assertEqual(len(prepare.call_args.args[0]), 1)
            self.assertEqual(prepare.call_args.kwargs["preset"].id, "piper")
        self.assertEqual(card.editor.toPlainText(), "De slimste")
        self.assertEqual(self.panel._run.row["id"], row["id"])
        self.assertEqual(practice_activity.read(self.db).answers, 0)

    def test_local_feedback_does_not_wait_for_a_remote_translation(self) -> None:
        """Keep local dictation responsive while the API lane is occupied; e.g. a slow English-to-Dutch correction cannot block listening."""

        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        provider = fixtures.FakeSentenceProvider()
        original = provider.grade

        def blocked(document):
            """Hold only the translation request; e.g. the local grade must arrive before this wait is released."""

            entered.set()
            release.wait(10)
            return original(document)

        provider.grade = blocked
        engine = sentence_service.SentenceService(self.db, provider)
        translated = self.db.sentence_current(engine.start(fixtures.options(), lambda _: None))
        self.db.sentence_submit(translated["id"], fixtures.DUTCH.replace("slimste", "slimmere"), 1200)
        library_checks.Checks.shared(self.db).resume(engine)
        self._wait(entered.is_set)
        first = self.panel._run.row["id"]
        self.panel._view.card.editor.setPlainText(fixtures.DUTCH)
        self.panel._view.action.click()
        self._wait(lambda: self.db.sentence_get(first)["outcome"] == "correct")
        self.assertIsNone(self.db.sentence_get(translated["id"])["outcome"])
        release.set()
        self._wait(lambda: self.db.sentence_get(translated["id"])["outcome"] == "wrong")

    def test_closing_during_audio_repair_cancels_without_late_widget_access(self) -> None:
        """Contain late worker callbacks after native deletion; e.g. closing while a voice is downloading cannot touch dead Qt labels."""

        card = self.panel._view.card
        self._wait(lambda: bool(card._player.calls))
        cached = self.db.listening_audio(self.panel._run.row["question_id"])
        self.db.listening_paths(cached["cache_key"])["normal"].unlink()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def blocked(_questions, report, **_kwargs):
            """Wait at a cooperative synthesis boundary; e.g. cancellation must be observed before publishing speech."""

            entered.set()
            release.wait(3)
            report("Resuming speech preparation")

        cache = SimpleNamespace(prepare=blocked, engine=self.engine.audio.engine)
        with mock.patch.object(card, "_cache", return_value=cache):
            card._replay.click()
            self._wait(entered.is_set)
            with mock.patch("sys.excepthook") as errors:
                self.dialog.close()
                self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
                self.assertFalse(shiboken6.isValid(card))
                release.set()
                self.assertTrue(QtCore.QThreadPool.globalInstance().waitForDone(4000))
                self.app.processEvents()
                errors.assert_not_called()

    def test_navigation_stops_speech_and_keeps_the_transcription_draft(self) -> None:
        """Pause without revealing answers or consuming another question; e.g. visiting Activity preserves the learner's unfinished transcription."""

        card = self.panel._view.card
        first = self.panel._run.row["id"]
        card.editor.setPlainText("De slimste buurman")
        previous = card._player.stops
        self.dialog._nav._buttons.button(library_practice.Page.ACTIVITY).click()
        self.app.processEvents()
        self.assertGreater(card._player.stops, previous)
        self.dialog._nav._buttons.button(library_practice.Page.LISTENING).click()
        self.assertEqual(self.panel._run.row["id"], first)
        self.assertEqual(card.editor.toPlainText(), "De slimste buurman")
        self.assertEqual(practice_activity.read(self.db).answers, 0)
