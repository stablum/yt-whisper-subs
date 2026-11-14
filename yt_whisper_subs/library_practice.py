"""Native vocabulary, verb and sentence practice with shared progress and activity.

Example: Library → Practice opens a configurable round or resumes an unfinished one.
"""

from __future__ import annotations

import time
import sqlite3
from datetime import datetime

from PySide6 import QtCore
from PySide6 import QtWidgets

from yt_whisper_subs import practice_round
from yt_whisper_subs import library_practice_progress
from yt_whisper_subs import library_practice_controls as controls
from yt_whisper_subs import library_practice_activity
from yt_whisper_subs import library_workers
from yt_whisper_subs import library_verbs
from yt_whisper_subs import library_verb_progress
from yt_whisper_subs import library_sentences
from yt_whisper_subs import library_sentence_progress
from yt_whisper_subs import practice
from yt_whisper_subs import practice_service


_STYLE = """
QLabel#practiceTitle { font-size: 24pt; font-weight: 700; color: #ffffff; }
QLabel#practicePrompt { font-size: 30pt; font-weight: 600; color: #ffd166; padding: 10px; }
QLabel#practiceContext { font-size: 13pt; color: #b8c7d9; padding: 6px; }
QLabel#practiceResult { font-size: 15pt; font-weight: 600; color: #dce6f2; padding: 8px 0; }
QLabel#practiceStats { font-size: 12pt; color: #c4d5e9; padding: 12px 0; }
QPushButton#practiceChoice { font-size: 17pt; text-align: left; padding: 12px 22px; border-radius: 10px; }
QPushButton#practiceChoice[answerState="correct"] { background: #214b3d; border: 2px solid #75dba4; color: #d2ffe5; }
QPushButton#practiceChoice[answerState="wrong"] { background: #552f39; border: 2px solid #ed8594; color: #ffe2e6; }
QToolButton#practiceOptions, QToolButton#practiceDetails { padding: 5px 10px; border-radius: 6px; }
"""


class PracticeDialog(QtWidgets.QDialog):
    """Open directly into practice while keeping persistence and question generation outside widgets."""

    activity_changed = QtCore.Signal()

    def __init__(self, service, parent=None, *, engine: practice_service.PracticeService | None = None) -> None:
        """Restore a resumable round before preparing another; e.g. opening Activity alone does not start practice."""

        super().__init__(parent)
        self.setWindowTitle("Practice · vocabulary, verbs and sentences")
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(1060, 900)
        self._db = service.db
        self._engine = engine or practice_service.PracticeService(self._db)
        self._session = self._db.practice_active()
        self._current: dict | None = None
        self._task: library_workers.BackgroundTask | None = None
        self._empty: practice_round.NothingToPractice | None = None
        self._started = 0.0
        self._autostart_pending = True
        self._closed = False
        self._arrange()
        self.setStyleSheet(_STYLE)
        self._controls.restore(self._db.setting("practice_options", ""))
        self._idle("Getting your next word ready…", "", "Continue")
        self._next.hide()
        QtWidgets.QApplication.instance().aboutToQuit.connect(self.reject)

    def _arrange(self) -> None:
        """Keep the question and continuation in stable positions; e.g. occasional setup lives in the options popover."""

        title = QtWidgets.QLabel("Practice")
        title.setObjectName("practiceTitle")
        self._controls = controls.RoundControls()
        self._status = QtWidgets.QLabel()
        self._status.setWordWrap(True)
        heading = QtWidgets.QHBoxLayout()
        heading.addWidget(self._status, 1)
        heading.addWidget(self._controls)
        self._bar = QtWidgets.QProgressBar()
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(6)
        self._surface = controls.ContinueSurface()
        self._surface.continued.connect(lambda: self._advance(guard=True))
        self._prompt = QtWidgets.QLabel()
        self._prompt.setObjectName("practicePrompt")
        self._context = controls.QuizContext()
        for label in (self._prompt, self._context):
            label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            label.setWordWrap(True)
        self._unsure = QtWidgets.QCheckBox("I'm unsure / guessing")
        unsure_size = self._unsure.sizePolicy()
        unsure_size.setRetainSizeWhenHidden(True)
        self._unsure.setSizePolicy(unsure_size)
        self._unsure.setToolTip("A correct guess gets a shorter review interval. Response speed does not affect grading.")
        self._answers = controls.AnswerButtons()
        self._answers.selected.connect(self._answer)
        self._result = QtWidgets.QLabel()
        self._result.setObjectName("practiceResult")
        self._result.setWordWrap(True)
        self._result.setMinimumHeight(40)
        self._explanation = QtWidgets.QLabel()
        self._explanation.setWordWrap(True)
        self._details = QtWidgets.QToolButton()
        self._details.setObjectName("practiceDetails")
        self._details.setMinimumHeight(28)
        self._details.setText("Why this answer?")
        self._details.setCheckable(True)
        self._details.toggled.connect(self._explanation.setVisible)
        self._hint = QtWidgets.QLabel()
        self._hint.setObjectName("settingsNote")
        self._hint.setWordWrap(True)
        for label in (self._prompt, self._result, self._explanation, self._hint, self._status):
            label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
            label.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        feedback = QtWidgets.QWidget()
        feedback.setMinimumHeight(130)
        feedback_layout = QtWidgets.QVBoxLayout(feedback)
        feedback_layout.setContentsMargins(0, 0, 0, 0)
        feedback_layout.addWidget(self._result)
        feedback_layout.addWidget(self._details, 0, QtCore.Qt.AlignmentFlag.AlignLeft)
        feedback_layout.addWidget(self._explanation)
        feedback_layout.addStretch()
        feedback_layout.addWidget(self._hint)
        body = QtWidgets.QVBoxLayout(self._surface)
        body.setContentsMargins(0, 12, 0, 0)
        body.setSpacing(12)
        for widget in (self._prompt, self._context, self._unsure, self._answers, feedback):
            body.addWidget(widget)
        body.addStretch()
        self._next = controls.ContinueButton()
        self._next.setObjectName("primaryButton")
        self._next.setMinimumHeight(44)
        self._next.setMinimumWidth(220)
        self._next.setAutoDefault(False)
        self._flag = QtWidgets.QPushButton("Report question")
        self._flag.setAutoDefault(False)
        self._cancel = QtWidgets.QPushButton("Cancel preparation")
        self._cancel.setAutoDefault(False)
        footer = QtWidgets.QHBoxLayout()
        footer.addWidget(self._flag)
        footer.addWidget(self._cancel)
        footer.addStretch()
        footer.addWidget(self._next)
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.setSpacing(12)
        layout.addLayout(heading)
        layout.addWidget(self._bar)
        layout.addWidget(self._surface, 1)
        layout.addLayout(footer)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        self._progress = library_practice_progress.ProgressPanel(self._db)
        self._verb_progress = library_verb_progress.VerbProgress(self._db)
        self._progress_tabs = QtWidgets.QTabWidget()
        self._progress_tabs.addTab(self._progress, "Vocabulary")
        self._progress_tabs.addTab(self._verb_progress, "Verbs")
        self._sentence_progress = library_sentence_progress.SentenceProgress(self._db)
        self._progress_tabs.addTab(self._sentence_progress, "Sentences")
        self._progress_tabs.currentChanged.connect(self._refresh_progress)
        self._activity = library_practice_activity.ActivityPanel(self._db)
        activity_scroll = QtWidgets.QScrollArea()
        activity_scroll.setWidgetResizable(True)
        activity_scroll.setWidget(self._activity)
        self._tabs = QtWidgets.QTabWidget()
        self._tabs.addTab(scroll, "Word quiz")
        self._tabs.addTab(self._progress_tabs, "Progress")
        self._tabs.addTab(activity_scroll, "Activity")
        self._verbs = library_verbs.VerbPanel(self._db)
        self._verbs.activity_changed.connect(self.activity_changed)
        self._tabs.addTab(self._verbs, "Verbs")
        self._sentences = library_sentences.SentencePanel(self._db)
        self._sentences.activity_changed.connect(self.activity_changed)
        self._tabs.addTab(self._sentences, "Sentences")
        self._tabs.currentChanged.connect(self._tab_changed)
        outer = QtWidgets.QVBoxLayout(self)
        outer.addWidget(title)
        outer.addWidget(self._tabs, 1)
        self._controls.restart_requested.connect(self._prepare)
        self._controls.finish_requested.connect(self._stop)
        self._controls.editing.connect(self._editing)
        self._next.clicked.connect(self._continue)
        self._flag.clicked.connect(self._report)
        self._cancel.clicked.connect(self._stop)

    def _idle(self, title: str, message: str, action: str) -> None:
        """Render a useful resting state with one primary action; e.g. another round or retry after an outage."""

        self._current = None
        self._task = None
        self._surface.reset()
        self._prompt.setText(title)
        self._context.message(message)
        self._result.clear()
        self._result.setStyleSheet("")
        self._details.setChecked(False)
        for widget in (self._bar, self._flag, self._cancel, self._unsure, self._answers, self._details, self._explanation):
            widget.hide()
        self._status.setText("Your answers are saved as you go.")
        self._hint.setText("")
        self._controls.setEnabled(True)
        self._controls.set_active(self._session is not None)
        self._next.setText(action)
        self._next.setEnabled(True)
        self._next.setDefault(True)
        self._next.show()
        self._next.setFocus()

    @property
    def _practicing(self) -> bool:
        """Use one visibility rule for every interaction; e.g. hidden pages cannot record an answer or exposure."""

        return not self._closed and self.isVisible() and self._tabs.currentIndex() == 0

    def _ensure_round(self) -> None:
        """Autostart only on a visible practice page; e.g. visiting Activity must not record hidden exposures."""

        if not self._practicing:
            return
        if self._controls.menu.isVisible():
            return
        if not self._autostart_pending or self._task or self._current:
            return
        self._autostart_pending = False
        if self._session:
            self._advance()
        else:
            self._prepare(cached_first=True)

    def _prepare(self, *, cached_first: bool = False, early: bool = False) -> None:
        """Prepare automatically without freezing the question surface; e.g. the first open reuses approved choices."""

        if self._task or self._closed:
            return
        opts = self._controls.options()
        self._db.set_setting("practice_options", opts.model_dump_json())
        if early:
            opts = opts.model_copy(update={"include_early": True})
        self._autostart_pending = False
        self._empty = None
        self._idle("Preparing your round…", "Getting the choices ready.", "Preparing…")
        self._controls.setEnabled(False)
        self._bar.setRange(0, 0)
        self._bar.show()
        self._next.setEnabled(False)
        self._next.setDefault(False)
        self._cancel.setEnabled(True)
        self._cancel.show()

        def prepare(report):
            """Return normal empty states as data; e.g. all caught up is not a network failure."""

            try:
                return self._engine.start(opts, report, cached_first=cached_first)
            except practice_round.NothingToPractice as empty:
                return empty

        task = library_workers.BackgroundTask(prepare)
        self._task = task
        task.signals.progress.connect(self._status.setText)
        task.signals.finished.connect(self._prepared)
        task.signals.failed.connect(self._failed)
        task.signals.cancelled.connect(self._cancelled)
        QtCore.QThreadPool.globalInstance().start(task)

    @QtCore.Slot(object)
    def _prepared(self, result) -> None:
        """Expose ready questions only when actually visible; e.g. finishing preparation while Activity is open adds no exposure."""

        if self._closed:
            return
        if self._task and self._task.cancel_requested:
            self._cancelled()
            return
        self._task = None
        if isinstance(result, practice_round.NothingToPractice):
            self._empty = result
            self._session = self._db.practice_active()
            titles = {"not_due": "You're all caught up", "no_words": "No words ready yet",
                      "not_enough": "Choose a smaller round", "no_questions": "Could not prepare this round"}
            action = "Practice early" if result.reason == "not_due" else "Try again"
            self._idle(titles[result.reason], str(result), "Resume round" if self._session else action)
            return
        self._session = result
        self._autostart_pending = True
        self._idle("Your round is ready", "Return to Word quiz to continue.", "Continue round")
        self._ensure_round()

    @QtCore.Slot(str, str)
    def _failed(self, message: str, _trace: str) -> None:
        """Offer an inline recovery while retaining the previous round; e.g. retry a failed generation request."""

        if not self._closed:
            self._session = self._db.practice_active()
            self._idle("Couldn't prepare this round", message, "Resume round" if self._session else "Try again")

    @QtCore.Slot()
    def _cancelled(self) -> None:
        """Leave a cancelled preparation paused rather than immediately restarting; e.g. keep a prior round resumable."""

        if not self._closed:
            self._session = self._db.practice_active()
            self._idle("Practice paused", "Your progress is saved.", "Resume round" if self._session else "Try again")

    def _advance(self, *, guard: bool = False) -> None:
        """Timestamp exactly one visible next question; e.g. answer and continuation are separate gestures."""

        if not self._session or not self._practicing:
            return
        self._current = self._db.practice_current(self._session)
        if self._current is None:
            self._finish(complete=True)
            return
        self._surface.reset()
        row = self._current
        summary = self._db.practice_summary(self._session)
        direction = practice.Direction(row["direction"])
        self._status.setText(f"{direction.label}    ·    {row['ordinal']} / {summary['planned']}    ·    {summary['correct']} correct")
        self._bar.setRange(0, summary["planned"])
        self._bar.setValue(row["ordinal"] - 1)
        self._bar.show()
        self._prompt.setText(row["content"]["prompt"])
        self._context.present(row)
        self._result.clear()
        self._result.setStyleSheet("")
        self._details.setChecked(False)
        self._details.hide()
        self._explanation.hide()
        self._unsure.setChecked(False)
        self._unsure.show()
        self._answers.present(row["choices"], guard=guard)
        self._controls.setEnabled(True)
        self._controls.set_active(True)
        self._next.setText("Choose an answer")
        self._next.setEnabled(False)
        self._next.setDefault(False)
        self._next.show()
        self._cancel.hide()
        self._flag.setEnabled(True)
        self._flag.show()
        self._hint.setText("1 / 2 / 3 to answer · Close anytime to pause")
        self._started = time.monotonic()

    def _answer(self, idx: int) -> None:
        """Save the first click and use later clicks to continue; e.g. feedback clicks cannot record another answer."""

        if not self._current or not self._practicing:
            return
        if self._current["outcome"] is not None:
            self._surface.request_continue()
            return
        try:
            elapsed = max(0, round((time.monotonic() - self._started) * 1000))
            row = self._db.practice_answer(self._current["id"], idx, self._unsure.isChecked(), elapsed)
        except (ValueError, OSError, sqlite3.Error) as exc:
            self._result.setText(f"Could not save your answer: {exc}")
            return
        self._current = row
        self.activity_changed.emit()
        if row["outcome"] == "invalid":
            self._report()
            return
        self._answers.reveal(row["correct_idx"], idx)
        self._unsure.hide()
        verdict = "Correct" if row["outcome"] == "correct" else "Not quite"
        answer = row["choices"][row["correct_idx"]]["text"]
        self._result.setText(f"{verdict} · {answer}")
        self._result.setStyleSheet("color: #9ae7b4;" if row["outcome"] == "correct" else "color: #ffd166;")
        explanation = row["content"]["explanation"]
        if row["outcome"] == "wrong":
            explanation += "\n" + row["choices"][idx]["explanation"]
        due = datetime.fromtimestamp(row["due_at"]).strftime("%d %b, %H:%M")
        self._explanation.setText(f"{explanation}\nNext review: {due}")
        self._details.show()
        self._bar.setValue(row["ordinal"])
        self._offer_continue()

    def _offer_continue(self) -> None:
        """Give mouse and keyboard the same deliberate next action; e.g. Space or Enter advances after feedback."""

        self._surface.arm()
        target = "Click the card or an answer again" if self._answers.isVisible() else "Click the card"
        self._hint.setText(f"{target} · Space / Enter to continue")
        self._next.setText("Continue →")
        self._next.setEnabled(True)
        self._next.setDefault(True)
        self._next.setFocus()

    def _continue(self) -> None:
        """Choose the appropriate primary action from saved state; e.g. opt into early practice explicitly."""

        if self._task or not self._practicing:
            return
        if self._current:
            self._surface.request_continue()
        elif self._session:
            self._advance()
        else:
            early = self._empty is not None and self._empty.reason == "not_due"
            self._prepare(early=early)

    def _editing(self, editing: bool) -> None:
        """Suspend answer/continue keys during native editing; e.g. typing 12 changes length instead of answering."""

        question = self._current is not None and self._current["outcome"] is None
        self._answers.shortcuts(question and not editing)
        self._next.setDefault(not editing and self._next.isEnabled())
        if not editing and self._autostart_pending:
            QtCore.QTimer.singleShot(0, self, self._ensure_round)

    def _report(self) -> None:
        """Keep reporting useful before or after answering; e.g. a quarantined question can be passed with one next click."""

        if self._current and self._flag.isEnabled():
            self._db.practice_flag(self._current["id"])
            self._current["outcome"] = "invalid"
            self.activity_changed.emit()
            self._result.setText("Question reported · excluded from grading")
            self._result.setStyleSheet("color: #c4d5e9;")
            self._answers.shortcuts(False)
            self._answers.hide()
            self._flag.setEnabled(False)
            self._unsure.hide()
            self._details.hide()
            self._explanation.hide()
            self._offer_continue()

    def _stop(self) -> None:
        """Cancel loading or deliberately end a round; e.g. ordinary closing still leaves it resumable."""

        self._controls.menu.close()
        if self._task:
            self._task.cancel()
            self._cancel.setEnabled(False)
            self._status.setText("Cancelling after the current request…")
        elif self._session:
            self._finish(complete=False)

    def _finish(self, *, complete: bool) -> None:
        """Show one concise completion card with the next action; e.g. move straight into another configured round."""

        result = self._db.practice_finish(self._session)
        complete = complete or result["correct"] + result["wrong"] + result["invalid"] == result["planned"]
        self._session = None
        self._empty = None
        counts = f"{result['correct']} correct · {result['wrong']} to revisit"
        if result["invalid"]:
            counts += f" · {result['invalid']} reported"
        self._idle("Round complete" if complete else "Round saved", counts, "Another round")

    def _tab_changed(self, idx: int) -> None:
        """Keep analytics on demand and practice automatic; e.g. no hidden question exposure while inspecting stats."""

        self._next.setDefault(idx == 0 and self._next.isEnabled())
        if idx in (0, 3, 4):
            self._db.set_setting("practice_last_mode", {0: "vocabulary", 3: "verbs", 4: "sentences"}[idx])
        if idx == 1:
            mode = {"vocabulary": 0, "verbs": 1, "sentences": 2}[self._db.setting("practice_last_mode", "vocabulary")]
            if self._progress_tabs.currentIndex() == mode:
                self._refresh_progress()
            else:
                self._progress_tabs.setCurrentIndex(mode)
        elif idx == 2:
            self._activity.refresh()
        elif idx == 3:
            self._verbs.activate()
        elif idx == 4:
            self._sentences.activate()
        else:
            self._ensure_round()

    def show_page(self, activity: bool = False) -> None:
        """Route every entry point to the intended page; e.g. a reminder's Practice now needs no further Start click."""

        if activity:
            if self._tabs.currentIndex() == 2:
                self._activity.refresh()
            else:
                self._tabs.setCurrentIndex(2)
        else:
            if self._db.setting("practice_last_mode", "vocabulary") == "sentences":
                self._tabs.setCurrentIndex(4)
                self._sentences.activate()
                return
            if self._db.setting("practice_last_mode", "vocabulary") == "verbs":
                self._tabs.setCurrentIndex(3)
                self._verbs.activate()
                return
            self._tabs.setCurrentIndex(0)
            if not self._current and not self._task:
                self._autostart_pending = True
                QtCore.QTimer.singleShot(0, self, self._ensure_round)

    def showEvent(self, event) -> None:
        """Let the window paint before preparing; e.g. no blocking or exposure during construction."""

        super().showEvent(event)
        QtCore.QTimer.singleShot(0, self, self._ensure_round)

    def _refresh_progress(self, *_args) -> None:
        """Keep every mode's learning in the main statistics page; e.g. show sentence accuracy without starting a round."""

        if self._progress_tabs.currentIndex() == 0:
            self._progress.refresh()
        elif self._progress_tabs.currentIndex() == 1:
            self._verb_progress.refresh()
        else:
            self._sentence_progress.refresh()

    def reject(self) -> None:
        """Pause on close, Escape or application exit; e.g. cancel an in-flight preparation without awaiting HTTP."""

        self._closed = True
        self._verbs.shutdown()
        self._sentences.shutdown()
        if self._task:
            self._task.cancel()
        super().reject()
