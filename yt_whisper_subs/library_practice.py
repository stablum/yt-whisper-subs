"""Native adaptive vocabulary practice with durable rounds and three large answer buttons.

Example: Library → Practice opens a configurable round or resumes an unfinished one.
"""

from __future__ import annotations

import textwrap
import time
import sqlite3
from datetime import datetime

from PySide6 import QtCore
from PySide6 import QtGui
from PySide6 import QtWidgets

from yt_whisper_subs import library_practice_progress
from yt_whisper_subs import library_workers
from yt_whisper_subs import practice
from yt_whisper_subs import practice_service


_STYLE = """
QLabel#practiceTitle { font-size: 24pt; font-weight: 700; color: #ffffff; }
QLabel#practicePrompt { font-size: 30pt; font-weight: 600; color: #ffd166; padding: 10px; }
QLabel#practiceContext { font-size: 13pt; color: #b8c7d9; padding: 6px; }
QLabel#practiceResult { font-size: 12pt; color: #dce6f2; padding: 8px; }
QLabel#practiceStats { font-size: 12pt; color: #c4d5e9; padding: 12px 0; }
QPushButton#practiceChoice { font-size: 17pt; text-align: left; padding: 12px 22px; border-radius: 10px; }
QPushButton#practiceChoice[answerState="correct"] { background: #214b3d; border: 2px solid #75dba4; color: #d2ffe5; }
QPushButton#practiceChoice[answerState="wrong"] { background: #552f39; border: 2px solid #ed8594; color: #ffe2e6; }
"""


class RoundControls(QtWidgets.QWidget):
    """Own editable round options and their validation; e.g. answer shortcuts never interfere with count entry."""

    def __init__(self) -> None:
        """Group native configuration controls; e.g. recognition and recall share the same bounded round length."""

        super().__init__()
        self.direction = QtWidgets.QComboBox()
        for direction in practice.Direction:
            self.direction.addItem(direction.label, direction)
        self.count = QtWidgets.QSpinBox()
        self.count.setRange(1, 50)
        self.count.setValue(10)
        self.count.setSuffix(" terms")
        self.early = QtWidgets.QCheckBox("Include words not due yet")
        self.start = QtWidgets.QPushButton("Start round")
        self.start.setObjectName("primaryButton")
        self.resume = QtWidgets.QPushButton("Resume round")
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        for widget in (self.direction, self.count, self.early, self.start, self.resume):
            layout.addWidget(widget)

    def options(self) -> practice.RoundOptions:
        """Convert Qt's string variant back into the domain enum; e.g. validate a twelve-term reverse round."""

        return practice.RoundOptions(direction=practice.Direction(self.direction.currentData()),
                                     count=self.count.value(), include_early=self.early.isChecked())

    def restore(self, document: str) -> None:
        """Ignore malformed preferences without hiding the controls; e.g. reopen a previously configured round."""

        try:
            opts = practice.RoundOptions.model_validate_json(document)
        except ValueError:
            return
        self.direction.setCurrentIndex(self.direction.findData(opts.direction))
        self.count.setValue(opts.count)
        self.early.setChecked(opts.include_early)


class AnswerButtons(QtWidgets.QWidget):
    """Keep three-choice presentation, keyboard access, and visual feedback in one cohesive control."""

    selected = QtCore.Signal(int)

    def __init__(self) -> None:
        """Bind shortcuts to actual buttons so visibility and enablement govern them; e.g. no hidden numeric shortcuts."""

        super().__init__()
        self._buttons = [QtWidgets.QPushButton() for _ in range(3)]
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        for idx, button in enumerate(self._buttons):
            button.setObjectName("practiceChoice")
            button.setMinimumHeight(88)
            button.setAutoDefault(False)
            button.setShortcut(QtGui.QKeySequence(str(idx + 1)))
            button.clicked.connect(lambda _checked=False, choice=idx: self.selected.emit(choice))
            layout.addWidget(button)

    def present(self, choices: list[dict]) -> None:
        """Reset only presentation state, preserving stored option order; e.g. a resumed question has identical choices."""

        self.setEnabled(True)
        for idx, button in enumerate(self._buttons):
            button.setText(f"{idx + 1}    {textwrap.fill(choices[idx]['text'], 55)}")
            self._style(button, "")
        self.show()

    def reveal(self, correct: int, selected: int) -> None:
        """Show the correct answer and the chosen error; e.g. the other distractor stays neutral."""

        self.setEnabled(False)
        for idx, button in enumerate(self._buttons):
            self._style(button, "correct" if idx == correct else ("wrong" if idx == selected else ""))

    @staticmethod
    def _style(button: QtWidgets.QPushButton, state: str) -> None:
        """Refresh Qt's dynamic selector consistently; e.g. remove the previous card's green highlight."""

        button.setProperty("answerState", state)
        button.style().unpolish(button)
        button.style().polish(button)


class PracticeDialog(QtWidgets.QDialog):
    """Own the current interaction while persistence and generation remain separate strategies."""

    def __init__(self, service, parent=None, *, engine: practice_service.PracticeService | None = None) -> None:
        """Bind one native window to a catalog; e.g. review while a video continues playing."""

        super().__init__(parent)
        self.setWindowTitle("Practice · vocabulary that stays with you")
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(1060, 900)
        self._db = service.db
        self._engine = engine or practice_service.PracticeService(self._db)
        self._session = self._db.practice_active()
        self._current: dict | None = None
        self._task: library_workers.BackgroundTask | None = None
        self._started = 0.0
        self._arrange()
        self.setStyleSheet(_STYLE)
        self._controls.restore(self._db.setting("practice_options", ""))
        self._idle()

    def _arrange(self) -> None:
        """Compose a spacious round surface and separate analytics tab; e.g. three accessible vertical choices."""

        title = QtWidgets.QLabel("Practice")
        title.setObjectName("practiceTitle")
        subtitle = QtWidgets.QLabel("Small rounds. Tricky choices. More time with the words that need it.")
        subtitle.setObjectName("settingsNote")
        self._controls = RoundControls()
        self._status = QtWidgets.QLabel()
        self._status.setWordWrap(True)
        self._bar = QtWidgets.QProgressBar()
        self._bar.setTextVisible(False)
        self._prompt = QtWidgets.QLabel("Your saved words, ready for practice.")
        self._prompt.setObjectName("practicePrompt")
        self._prompt.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self._prompt.setWordWrap(True)
        self._context = QtWidgets.QLabel("Choose a direction and round length above.")
        self._context.setObjectName("practiceContext")
        self._context.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self._context.setWordWrap(True)
        self._unsure = QtWidgets.QCheckBox("I'm unsure / guessing")
        self._unsure.setToolTip("A correct guess gets a shorter review interval. Response speed does not affect grading.")
        self._answers = AnswerButtons()
        self._answers.selected.connect(self._answer)
        self._result = QtWidgets.QLabel()
        self._result.setObjectName("practiceResult")
        self._result.setWordWrap(True)
        for label in (self._prompt, self._context, self._result, self._status):
            label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self._next = QtWidgets.QPushButton("Next term →")
        self._next.setObjectName("primaryButton")
        self._flag = QtWidgets.QPushButton("Report unclear question")
        self._end = QtWidgets.QPushButton("End round")
        actions = QtWidgets.QHBoxLayout()
        actions.addWidget(self._flag)
        actions.addWidget(self._end)
        actions.addStretch()
        actions.addWidget(self._next)
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.setSpacing(12)
        layout.addWidget(self._controls)
        for widget in (self._status, self._bar, self._prompt, self._context, self._unsure, self._answers, self._result):
            layout.addWidget(widget)
        layout.addStretch()
        layout.addLayout(actions)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        self._progress = library_practice_progress.ProgressPanel(self._db)
        self._tabs = QtWidgets.QTabWidget()
        self._tabs.addTab(scroll, "Practice")
        self._tabs.addTab(self._progress, "Progress")
        self._tabs.currentChanged.connect(self._tab_changed)
        outer = QtWidgets.QVBoxLayout(self)
        outer.addWidget(title)
        outer.addWidget(subtitle)
        outer.addWidget(self._tabs, 1)
        self._controls.start.clicked.connect(self._prepare)
        self._controls.resume.clicked.connect(self._advance)
        self._next.clicked.connect(self._advance)
        self._flag.clicked.connect(self._report)
        self._end.clicked.connect(self._stop)

    def _idle(self, message: str = "") -> None:
        """Offer new and resumed rounds without showing uncounted questions; e.g. all due reviews are finished."""

        self._current = None
        self._task = None
        self._bar.hide()
        self._controls.resume.setVisible(self._session is not None)
        self._next.hide()
        self._flag.hide()
        self._end.hide()
        self._unsure.hide()
        self._answers.hide()
        self._controls.setEnabled(True)
        self._status.setText(message or "Due reviews come first, with room for new words. Close anytime and resume later.")

    def _prepare(self) -> None:
        """Run generation off the GUI thread; e.g. an API timeout never freezes playback or library browsing."""

        opts = self._controls.options()
        self._db.set_setting("practice_options", opts.model_dump_json())
        self._controls.setEnabled(False)
        self._bar.setRange(0, 0)
        self._bar.show()
        self._end.setText("Cancel preparation")
        self._end.show()
        self._status.setText("Preparing challenging choices and checking that only one answer fits…")
        task = library_workers.BackgroundTask(lambda report: self._engine.start(opts, report))
        self._task = task
        task.signals.progress.connect(self._status.setText)
        task.signals.finished.connect(self._prepared)
        task.signals.failed.connect(self._failed)
        task.signals.cancelled.connect(self._cancelled)
        QtCore.QThreadPool.globalInstance().start(task)

    @QtCore.Slot(object)
    def _prepared(self, session_id: str) -> None:
        """Open only a fully prepared durable round; e.g. rejected drafts reduce the announced round length."""

        self._task = None
        self._session = session_id
        self._advance()

    @QtCore.Slot(str, str)
    def _failed(self, message: str, _trace: str) -> None:
        """Keep all historical progress after preparation errors; e.g. show missing-key or network failures inline."""

        self._idle(f"Could not prepare this round: {message}")

    @QtCore.Slot()
    def _cancelled(self) -> None:
        """Return after cooperative cancellation; e.g. an in-flight API request may finish first."""

        self._session = self._db.practice_active()
        self._idle("Preparation cancelled. Your existing progress is safe.")

    def _advance(self) -> None:
        """Present and timestamp exactly one next question; e.g. resuming does not inflate its exposure count."""

        if not self._session:
            return
        self._current = self._db.practice_current(self._session)
        if self._current is None:
            self._stop()
            return
        row = self._current
        content = row["content"]
        summary = self._db.practice_summary(self._session)
        direction = practice.Direction(row["direction"])
        self._status.setText(f"{direction.label}    ·    Term {row['ordinal']} of {summary['planned']}    ·    "
                             f"{summary['correct']} correct / {summary['wrong']} to revisit")
        if summary["planned"] < row["requested_count"]:
            self._status.setText(self._status.text() + f"    ·    {summary['planned']} available today (requested {row['requested_count']})")
        self._bar.setRange(0, summary["planned"])
        self._bar.setValue(row["ordinal"] - 1)
        self._bar.show()
        self._prompt.setText(content["prompt"])
        self._context.setText(content["context"])
        self._result.clear()
        self._unsure.setChecked(False)
        self._unsure.setEnabled(True)
        self._unsure.show()
        self._answers.present(row["choices"])
        self._controls.setEnabled(False)
        self._controls.resume.hide()
        self._next.hide()
        self._flag.setEnabled(True)
        self._flag.show()
        self._end.setText("End round")
        self._end.show()
        self._started = time.monotonic()

    def _answer(self, idx: int) -> None:
        """Persist before revealing feedback; e.g. repeated activation cannot count as a second success."""

        if not self._current or self._current["outcome"] is not None:
            return
        try:
            elapsed = max(0, round((time.monotonic() - self._started) * 1000))
            row = self._db.practice_answer(self._current["id"], idx, self._unsure.isChecked(), elapsed)
        except (ValueError, OSError, sqlite3.Error) as exc:
            self._result.setText(f"Could not save your answer: {exc}")
            return
        self._current = row
        if row["outcome"] == "invalid":
            self._report()
            return
        self._unsure.setEnabled(False)
        self._answers.reveal(row["correct_idx"], idx)
        verdict = "Correct" if row["outcome"] == "correct" else "A word to revisit"
        explanation = row["content"]["explanation"]
        if row["outcome"] == "wrong":
            explanation += "\n" + row["choices"][idx]["explanation"]
        due = datetime.fromtimestamp(row["due_at"]).strftime("%d %b, %H:%M")
        self._result.setText(f"{verdict} · {row['content']['surface']} → {row['content']['meaning']}\n{explanation}\nNext review: {due}")
        self._next.show()
        self._next.setFocus()

    def _report(self) -> None:
        """Exclude defective AI content before or after answering; e.g. a plausible synonym was actually valid."""

        if self._current:
            self._db.practice_flag(self._current["id"])
            self._current["outcome"] = "invalid"
            self._result.setText("Question reported and removed from future practice. It does not affect your score or review schedule.")
            self._flag.setEnabled(False)
            self._unsure.setEnabled(False)
            self._answers.setEnabled(False)
            self._next.show()

    def _stop(self) -> None:
        """Save completed or partial results without penalizing unseen terms; e.g. leave a round early."""

        if self._task:
            self._task.cancel()
            self._status.setText("Cancelling preparation after the current request…")
            return
        if self._session:
            result = self._db.practice_finish(self._session)
            self._session = None
            self._prompt.setText("Round saved")
            self._context.setText(f"{result['correct']} correct · {result['wrong']} to revisit · {result['invalid']} reported")
            self._result.clear()
            self._idle("Your next round will adapt to these results. See Progress for your history.")

    def _tab_changed(self, idx: int) -> None:
        """Refresh analytics on demand; e.g. inspect the result of the answer just committed."""

        if idx == 1:
            self._progress.refresh()

    def closeEvent(self, event) -> None:
        """Leave active rounds resumable and cancel preparation; e.g. never wait for HTTP before closing."""

        if self._task:
            self._task.cancel()
        super().closeEvent(event)
