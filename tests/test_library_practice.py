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
from PySide6 import QtTest
from PySide6 import QtWidgets

from tests import practice_fixtures
from yt_whisper_subs import library_db
from yt_whisper_subs import library_practice
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

    def setUp(self) -> None:
        """Build a complete catalog and injectable strategy; e.g. no API key or user's database is required."""

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = library_db.LibraryDb(Path(tmp.name) / "catalog.sqlite3")
        self.db.initialize()
        practice_fixtures.seed(self.db)
        self.engine = practice_service.PracticeService(self.db, practice_fixtures.FakeGenerator())
        self.dialog = self._dialog()
        self.addCleanup(lambda: self.dialog.close())

    def _dialog(self):
        """Render actual native widgets without showing desktop windows; e.g. use Windows font rendering for previews."""

        dialog = library_practice.PracticeDialog(SimpleNamespace(db=self.db), engine=self.engine)
        dialog.setStyleSheet(library_theme.STYLE + library_practice._STYLE)
        dialog.setAttribute(QtCore.Qt.WidgetAttribute.WA_DontShowOnScreen)
        dialog.show()
        self.app.processEvents()
        return dialog

    def _wait_ready(self) -> None:
        """Wait for the worker's meaningful completion; e.g. avoid sleep-only assertions about API preparation."""

        deadline = time.monotonic() + 5
        while self.dialog._current is None:
            QtTest.QTest.qWait(10)
            if time.monotonic() > deadline:
                self.fail(self.dialog._status.text())

    def test_answer_report_and_next_use_real_saved_history(self) -> None:
        """A wrong answer highlights feedback, reporting removes it, and Next presents a different word."""

        self.dialog._controls.count.setValue(3)
        self.dialog._controls.start.click()
        self._wait_ready()
        row = self.dialog._current
        wrong = (row["correct_idx"] + 1) % 3
        QtTest.QTest.mouseClick(self.dialog._answers._buttons[wrong], QtCore.Qt.MouseButton.LeftButton)
        self.assertIn("A word to revisit", self.dialog._result.text())
        self.assertTrue(all(not button.isEnabled() for button in self.dialog._answers._buttons))
        self.assertEqual(practice_stats.snapshot(self.db)["totals"]["wrong"], 1)
        self.dialog._flag.click()
        self.assertEqual(practice_stats.snapshot(self.db)["totals"]["attempts"], 0)
        self.dialog._next.click()
        self.assertNotEqual(row["word_id"], self.dialog._current["word_id"])
        self.dialog._unsure.setChecked(True)
        self.dialog._answers._buttons[self.dialog._current["correct_idx"]].click()
        self.assertEqual(self.dialog._current["rating"], 2)
        self.dialog._end.click()
        self.assertIn("Round saved", self.dialog._prompt.text())
        self.dialog._tabs.setCurrentIndex(1)
        self.assertGreater(self.dialog._progress._terms.rowCount(), 0)

    def test_close_and_resume_does_not_recount_the_current_question(self) -> None:
        """Closing the dialog preserves the round; e.g. continue later with the same button ordering."""

        self.dialog._controls.start.click()
        self._wait_ready()
        row = self.dialog._current
        self.dialog.close()
        self.app.processEvents()
        self.dialog = self._dialog()
        self.dialog._controls.resume.click()
        self.assertEqual(self.dialog._current["id"], row["id"])
        self.assertEqual(self.dialog._current["choices"], row["choices"])
        self.assertEqual(practice_stats.snapshot(self.db)["totals"]["shown"], 1)

    def test_answer_shortcuts_do_not_intercept_round_length_entry(self) -> None:
        """Numeric answer access must respect native editing; e.g. type a twelve-term round before starting."""

        editor = self.dialog._controls.count.lineEdit()
        editor.selectAll()
        QtTest.QTest.keyClicks(editor, "12")
        self.assertEqual(self.dialog._controls.count.value(), 12)

    def test_closing_during_generation_cancels_without_a_partial_round(self) -> None:
        """A delayed provider must not hold the window open or invent exposures; e.g. close during an API call."""

        entered, release = threading.Event(), threading.Event()
        generate = self.engine._generator.generate

        def blocked(rows, direction):
            """Simulate an uninterruptible HTTP segment; e.g. cancellation is checked at the next report."""

            entered.set()
            release.wait(3)
            return generate(rows, direction)

        self.engine._generator.generate = blocked
        self.dialog._controls.start.click()
        deadline = time.monotonic() + 4
        while not entered.is_set() and time.monotonic() < deadline:
            QtTest.QTest.qWait(10)
        self.assertTrue(entered.is_set())
        cancelled = []
        self.dialog._task.signals.cancelled.connect(lambda: cancelled.append(True))
        self.dialog.close()
        release.set()
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
        self.dialog._controls.start.click()
        self._wait_ready()
        self.assertNotIn(self.dialog._current["content"]["surface"], self.dialog._context.text())
        if folder := os.environ.get("YTW_PRACTICE_PREVIEW"):
            self.app.processEvents()
            self.assertTrue(self.dialog.grab().save(str(Path(folder) / "practice-round.png")))
        self.dialog._tabs.setCurrentIndex(1)
        self.app.processEvents()
        self.assertEqual(self.dialog._progress._terms.rowCount(), 8)
        self.assertEqual(self.dialog._progress._rounds.rowCount(), 13)
        self.dialog._progress._terms.sortItems(5, QtCore.Qt.SortOrder.AscendingOrder)
        table = self.dialog._progress._terms
        keys = [table.item(idx, 5).data(QtCore.Qt.ItemDataRole.UserRole) for idx in range(table.rowCount())]
        self.assertEqual(keys, sorted(keys))
        if folder:
            self.assertTrue(self.dialog.grab().save(str(Path(folder) / "practice-progress.png")))
