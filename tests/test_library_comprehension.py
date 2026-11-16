"""Exercise native passage practice without remote models or audible speech.

Example: an open answer remains queued while the learner continues, and listening feedback cannot leak the passage early.
"""

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6 import QtCore
from PySide6 import QtWidgets

from tests import comprehension_fixtures as fixtures
from tests import listening_fixtures as speech
from tests import sentence_fixtures
from yt_whisper_subs import comprehension as comp
from yt_whisper_subs import comprehension_service
from yt_whisper_subs import library_checks
from yt_whisper_subs import library_comprehension
from yt_whisper_subs import library_db
from yt_whisper_subs import library_listening
from yt_whisper_subs import practice_activity


class ComprehensionFlowTests(unittest.TestCase):
    """Drive real widgets, queues and SQLite; e.g. answering is independent of remote correction latency."""

    @classmethod
    def setUpClass(cls):
        """Share Qt's application lifetime; e.g. delayed feedback uses normal queued signals."""

        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        """Seed synthetic vocabulary and hold every created view; e.g. cleanup drains workers before removing their catalog."""

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = library_db.LibraryDb(Path(folder.name) / 'catalog.sqlite3')
        self.db.initialize()
        sentence_fixtures.seed(self.db)
        self.provider, self.speech = fixtures.Provider(), speech.Speech()
        self.engine = comprehension_service.ComprehensionService(self.db, self.provider, speech=self.speech)
        self.panels, self.release = [], threading.Event()
        self.addCleanup(self._close)

    def _close(self):
        """Release held requests before destroying QObject receivers; e.g. an in-flight grade cannot touch a deleted view."""

        self.release.set()
        for panel in self.panels:
            panel.shutdown()
            panel.close()
        library_checks.Checks.shared(self.db, kind='comprehension').stop()
        QtCore.QThreadPool.globalInstance().waitForDone(5000)
        self.app.processEvents()
        for panel in self.panels:
            panel.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def _wait(self, predicate):
        """Wait for a concrete UI state with a bounded deadline; e.g. preparation failures cannot spend an API timeout."""

        until = time.monotonic() + 6
        while not predicate() and time.monotonic() < until:
            self.app.processEvents()
            time.sleep(.01)
        self.assertTrue(predicate())

    def _panel(self, *, direction=comp.Direction.READ, mc=2, open=1):
        """Consume a prebuilt round through production activation; e.g. the first question appears without Start."""

        opts = fixtures.options(direction=direction, mc=mc, open=open)
        self.engine.start(opts, lambda _: None, background=True)
        key = f'comprehension:{direction.value}_options'
        self.db.set_setting(key, opts.model_dump_json())
        with mock.patch.object(library_listening, 'WavePlayer', speech.Player):
            panel = library_comprehension.Panel(self.db, direction, engine=self.engine)
        errors, failed = [], panel._failed

        def capture(message, trace=''):
            """Preserve actionable failure evidence; e.g. preparation errors are deliberately generic in the listening UI."""

            errors.append(trace or message)
            failed(message, trace)

        panel._failed = capture
        panel.setAttribute(QtCore.Qt.WidgetAttribute.WA_DontShowOnScreen)
        panel.resize(1280, 820)
        self.panels.append(panel)
        panel.show()
        try:
            self._wait(lambda: panel._run.row is not None)
        except AssertionError:
            self.fail('\n'.join(errors) or str(panel._run))
        return panel

    def _answer(self, panel):
        """Submit the current fixture format; e.g. a choice is local while an open answer is asynchronous."""

        row = panel._run.row
        if row['kind'] == 'mc':
            panel._choose(row['content']['correct_idx'])
        else:
            panel._view.card.editor.setPlainText('De gemeente wil minder verkeer.')
            panel._view.action.click()
        return row['id']

    def test_reading_opens_immediately_and_review_walks_in_order(self):
        """Retain explicit feedback transitions and a fixed footer; e.g. a long correction cannot hide the primary action."""

        panel = self._panel()
        view = panel._view
        self.assertIn(fixtures.PARAGRAPHS[0], view.source.browser.toPlainText())
        self.assertEqual(len(view.card.choices.findChildren(QtWidgets.QPushButton)), 4)
        first = self._answer(panel)
        self.assertEqual(panel._run.row['id'], first)
        self.assertIn('100%', view.card._verdict.text())
        panel._next()
        second = self._answer(panel)
        panel._next()
        third = self._answer(panel)
        self._wait(lambda: self.db.comprehension_get(third)['grade'] is not None)
        panel._jump(first)
        panel._next()
        self.assertEqual(panel._run.row['id'], second)
        view.card._details.setText('Lange uitleg. ' * 500)
        self.app.processEvents()
        self.assertTrue(panel.rect().contains(view.action.mapTo(panel, view.action.rect().bottomRight())))
        self.assertEqual(practice_activity.read(self.db).answers, 3)

    def test_pending_open_answer_does_not_block_or_replace_next_draft(self):
        """Let the learner keep writing during grading; e.g. a late answer-one correction leaves answer two untouched."""

        panel = self._panel(mc=0, open=3)
        entered = threading.Event()
        grade = self.provider.grade

        def blocked(document):
            """Hold one semantic request; e.g. another question must remain interactive before release."""

            entered.set()
            self.release.wait(4)
            return grade(document)

        self.provider.grade = blocked
        first = self._answer(panel)
        self._wait(entered.is_set)
        second = panel._run.row['id']
        self.assertNotEqual(second, first)
        panel._view.card.editor.setPlainText('Mijn onafgemaakte tweede antwoord')
        self.release.set()
        self._wait(lambda: self.db.comprehension_get(first)['grade'] is not None)
        self.assertEqual(panel._run.row['id'], second)
        self.assertEqual(panel._view.card.editor.toPlainText(), 'Mijn onafgemaakte tweede antwoord')
        panel.hide()
        panel.show()
        self.app.processEvents()
        self.assertEqual(self.db.comprehension_draft(second)['text'], 'Mijn onafgemaakte tweede antwoord')

    def test_listening_withholds_source_and_feedback_until_all_submissions(self):
        """Prevent question feedback from answering later questions; e.g. even a correct choice reveals no evidence early."""

        panel = self._panel(direction=comp.Direction.LISTEN, mc=2, open=0)
        view, source = panel._view, panel._view.source
        self._wait(lambda: bool(source.audio._player.calls))
        self.assertEqual(source.browser.toPlainText(), '')
        self.assertFalse(source.reveal.isEnabled())
        source._reveal()
        self.assertEqual(source.browser.toPlainText(), '')
        first = self._answer(panel)
        self.assertIsNone(self.db.comprehension_get(first)['viewed_at'])
        self.assertFalse(view.card._correction.isVisible())
        self.assertNotIn('100%', view.status.text())
        panel._next()
        self._answer(panel)
        self.assertTrue(source.reveal.isEnabled())
        self.assertEqual(source.browser.toPlainText(), '')
        source.reveal.click()
        self.assertIn(fixtures.PARAGRAPHS[0], source.browser.toPlainText())
        self.assertIsNotNone(self.db.comprehension_get(first)['viewed_at'])
        before = len(source.audio._player.calls)
        source.audio._player.finished.emit()
        self.assertEqual(len(source.audio._player.calls), before + 1)
        panel.hide()
        self.assertFalse(source.audio._playing)
        self.assertEqual(practice_activity.read(self.db).answers, 2)

    def test_audio_repair_preserves_same_question_and_typed_draft(self):
        """Repair only local speech when a WAV disappears; e.g. a narrator failure never buys replacement passage text."""

        panel = self._panel(direction=comp.Direction.LISTEN, mc=0, open=2)
        audio = panel._view.source.audio
        self._wait(lambda: bool(audio._player.calls))
        panel._view.card.editor.setPlainText('Mijn voorlopige antwoord')
        row_id = panel._run.row['id']
        audio._rows[0]['paths']['normal'].unlink()
        before = len(audio._player.calls)
        audio.restart()
        self._wait(lambda: len(audio._player.calls) > before)
        self.assertEqual(panel._run.row['id'], row_id)
        self.assertEqual(panel._view.card.editor.toPlainText(), 'Mijn voorlopige antwoord')
        self.assertEqual(len(self.provider.generated), 1)

    def test_reopening_prioritizes_unseen_correction_over_seen_cursor_and_spare(self):
        """Keep review backlogs visible across windows; e.g. a saved position on an old correction must not bury the next unseen one."""

        opts = fixtures.options(mc=2, open=0)
        session = self.engine.start(opts, lambda _: None)
        rows = self.db.comprehension_history(session)
        for row in rows:
            self.db.comprehension_open(row['id'])
            self.db.comprehension_submit(row['id'], row['content']['correct_idx'], 1000)
        self.db.comprehension_seen(rows[0]['id'])
        self.db.set_setting('comprehension:read_options_cursor', json.dumps(dict(id=rows[0]['id'])))
        panel = self._panel(mc=2, open=0)
        self.assertEqual(panel._run.session, session)
        self.assertEqual(panel._run.row['id'], rows[1]['id'])


if __name__ == '__main__':
    unittest.main()
