"""Exercise checked stock, local scoring and the native Sentence builder board."""

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from PySide6 import QtCore, QtGui, QtTest, QtWidgets

from tests import sentence_fixtures
from yt_whisper_subs import library_db
from yt_whisper_subs import library_order_blocks
from yt_whisper_subs import library_word_order
from yt_whisper_subs import practice_activity
from yt_whisper_subs import word_order
from yt_whisper_subs import word_order_service


class Provider:
    """Supply deterministic clauses and two independently accepted orders."""

    model, reasoning = "fixture-sentence-builder", "low"
    available = True

    def generate(self, targets, extras):
        """Use the selected word in a subordinate clause."""

        if not self.available:
            raise AssertionError("A prepared round must not call the model")
        drafts = []
        for target in targets:
            tiles = ["ik blijf thuis", "omdat", "mijn buurman", f"graag wil {target['lemma']}", "als het regent"]
            drafts.append(word_order.Draft(word_id=target["word_id"], english="I stay home because my neighbour likes to walk when it rains.",
                                           dutch=" ".join(tiles).capitalize() + ".", tiles=tiles,
                                           extras=[word_order.Extra(text=x, why="This changes the meaning.") for x in ["toch", "voordat"][:extras]],
                                           pattern=target["pattern"], explanation="After omdat, the finite verb follows the subject.",
                                           target_form=target["lemma"]))
        return drafts

    def audit(self, proposals):
        """Approve a same-membership alternative, even if the canonical order differs."""

        return [word_order.Verdict(word_id=draft.word_id, valid=True, reason="Grounded", orders=[[0, 1, 2, 3, 4], [0, 1, 4, 2, 3]])
                for _, draft in proposals]


class OrderTests(unittest.TestCase):
    """Exercise the new round through the shared selector, stock and activity boundaries."""

    def setUp(self):
        """Use one isolated catalog with the existing synthetic vocabulary."""

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = library_db.LibraryDb(Path(folder.name) / "catalog.sqlite3")
        self.db.initialize()
        sentence_fixtures.seed(self.db)
        self.provider = Provider()
        self.engine = word_order_service.Service(self.db, self.provider)

    def test_stock_consumption_alternate_and_independent_activity(self):
        """A valid alternative is fully correct and updates only the new practice history."""

        opts = word_order.Options(count=1, extras=1)
        self.engine.start(opts, lambda _: None, background=True)
        self.assertIsNone(self.db.word_order_active())
        self.provider.available = False
        session = self.engine.start(opts, lambda _: None)
        row = self.db.word_order_current(session)
        trial = [0, 1, 4, 2, 3]
        self.db.word_order_draft(row["id"], trial)
        self.assertEqual(json.loads(self.db.word_order_current(session)["draft_json"]), trial)
        answer = self.db.word_order_answer(row["id"], trial, 1200)
        self.assertEqual(answer["outcome"], "correct")
        self.assertEqual(self.db.word_order_answer(row["id"], [3, 0], 900), answer)
        self.assertEqual(self.db.word_order_resume(), session)
        self.db.word_order_viewed(row["id"])
        self.assertIsNone(self.db.word_order_resume())
        self.assertEqual(practice_activity.read(self.db).week[-1].word_order, 1)
        self.assertTrue(all(row["attempts"] == 0 for row in self.db.word_candidates("en_nl")))

    def test_omitted_and_extra_blocks_get_partial_credit(self):
        """An otherwise plausible order can receive partial credit and specific exclusions."""

        session = self.engine.start(word_order.Options(count=1, extras=2), lambda _: None)
        row = self.db.word_order_current(session)
        answer = self.db.word_order_answer(row["id"], [0, 1, 3, 2, 5], 1100)
        grade = word_order.Grade.model_validate_json(answer["grade_json"])
        self.assertFalse(grade.correct)
        self.assertEqual(grade.excluded, [5])
        self.assertEqual(grade.omitted, [4])
        self.assertAlmostEqual(grade.partial, 0.6)
        verdict = word_order.Verdict.model_validate_json(row["validation_json"])
        verdict.orders.append([0, 1, 2, 3, 5])
        with self.assertRaises(ValueError):
            verdict.check(word_order.Draft.model_validate_json(row["body_json"]))

    def test_click_drop_and_animated_feedback(self):
        """Click blocks, reorder through the drop handler, then verify native correction and saved draft."""

        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        app.setQuitOnLastWindowClosed(False)
        opts = word_order.Options(count=1, extras=2)
        self.engine.start(opts, lambda _: None)
        panel = library_word_order.Panel(self.db, engine=self.engine)
        self.addCleanup(panel.close)
        panel.resize(850, 680)
        panel.setAttribute(QtCore.Qt.WidgetAttribute.WA_DontShowOnScreen)
        panel.show()
        panel.activate()
        app.processEvents()
        bank, answer = panel._board._bank, panel._board._answer
        for idx in (0, 1, 5):
            item = next(bank.item(row) for row in range(bank.count()) if bank.item(row).data(library_order_blocks._ROLE) == idx)
            QtTest.QTest.mouseClick(bank.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=bank.visualItemRect(item).center())
            app.processEvents()
        self.assertEqual(panel._board.selected, [0, 1, 5])
        point = answer.visualItemRect(answer.item(1)).center()
        mouse = QtCore.Qt.MouseButton.LeftButton
        modifiers = QtCore.Qt.KeyboardModifier.NoModifier
        with mock.patch.object(answer, "startDrag") as started:
            QtTest.QTest.mousePress(answer.viewport(), mouse, pos=point)
            destination = point + QtCore.QPoint(QtWidgets.QApplication.startDragDistance() + 12, 0)
            event = QtGui.QMouseEvent(QtCore.QEvent.Type.MouseMove, QtCore.QPointF(destination),
                                      QtCore.QPointF(answer.viewport().mapToGlobal(destination)),
                                      QtCore.Qt.MouseButton.NoButton, mouse, modifiers)
            app.sendEvent(answer.viewport(), event)
            started.assert_called_once()
        mime = QtCore.QMimeData()
        mime.setData(library_order_blocks._MIME, b"1")
        left = answer.visualItemRect(answer.item(0)).topLeft() + QtCore.QPoint(2, 6)
        event = SimpleNamespace(source=lambda: answer, mimeData=lambda: mime, position=lambda: QtCore.QPointF(left),
                                acceptProposedAction=lambda: None, isAccepted=lambda: True)
        answer.dropEvent(event)
        self.assertEqual(panel._board.selected, [1, 0, 5])
        self.assertEqual(json.loads(self.db.word_order_current(panel.round_id)["draft_json"]), [1, 0, 5])
        panel._check.click()
        app.processEvents()
        self.assertTrue(panel._moves.isVisible())
        self.assertIn("Leave out", panel._note.text())
        self.assertFalse(panel._moves.grab().isNull())
        panel._next.click()
        self.assertTrue(panel._finished)
