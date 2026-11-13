"""Native typed verb practice lifecycle, beside vocabulary and shared activity.

Example: select Verbs to resume a saved sentence or automatically prepare the first exercise.
"""

from __future__ import annotations

import sqlite3
import time

import pydantic as pd
from PySide6 import QtCore
from PySide6 import QtWidgets

from yt_whisper_subs import library_verb_controls as controls
from yt_whisper_subs import library_verb_progress
from yt_whisper_subs import library_workers
from yt_whisper_subs import practice_service
from yt_whisper_subs import verb_practice as verbs
from yt_whisper_subs import verb_service


class _Run(pd.BaseModel):
    """Group transient round state separately from widgets; e.g. pending answers stay represented by the DB row."""

    session: str | None = None
    row: dict | None = None
    started: float = 0
    guard_until: float = 0
    pending: bool = True
    closed: bool = False
    operation: str = ""
    empty: str = ""


class VerbPanel(QtWidgets.QWidget):
    """Coordinate a typed exercise through replaceable domain services and durable history."""

    activity_changed = QtCore.Signal()

    def __init__(self, db, *, engine: verb_service.VerbService | None = None) -> None:
        """Construct without generation or exposure; e.g. opening Activity never starts a verb round."""

        super().__init__()
        self._db = db
        self._engine = engine or verb_service.VerbService(db)
        self._run = _Run(session=db.verb_active())
        self._task: library_workers.BackgroundTask | None = None
        self._view = controls.VerbView(db.setting("verb_options", ""))
        self._progress = library_verb_progress.VerbProgress(db)
        self._tabs = QtWidgets.QTabWidget()
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._view)
        self._tabs.addTab(scroll, "Rewrite sentences")
        self._tabs.addTab(self._progress, "Verb insights")
        self._tabs.currentChanged.connect(self._tab_changed)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._tabs)
        self._view.action.clicked.connect(self._action)
        self._view.card.submitted.connect(self._submit)
        self._view.card.editor.textChanged.connect(self._input_changed)
        self._view.card.continued.connect(lambda: self._advance(guard=True))
        self._view.flag.clicked.connect(self._report)
        self._view.cancel.clicked.connect(self._stop)
        self._view.options.restart_requested.connect(self._prepare)
        self._view.options.finish_requested.connect(self._stop)
        self._view.options.editing.connect(self._editing)
        self._idle("Verb transformations", "Rewrite a Dutch sentence for a new subject, tense or situation.", "Continue")
        self._view.action.hide()

    @property
    def _active(self) -> bool:
        """Protect against hidden question exposure; e.g. preparation may finish while viewing the calendar."""

        return not self._run.closed and self.isVisible() and self._tabs.currentIndex() == 0

    def activate(self) -> None:
        """Automatically open the first visible question once; e.g. switching tabs needs no Start round click."""

        if not self._active or self._task or self._view.options.menu.isVisible():
            return
        if self._run.row:
            answered = self._run.row["outcome"] is not None
            self._view.action.setDefault(answered)
            if answered:
                self._view.action.setFocus()
            return
        if not self._run.pending:
            return
        self._run.pending = False
        if self._run.session:
            self._advance()
        else:
            self._prepare(cached_first=True)

    def _idle(self, title: str, message: str, action: str) -> None:
        """Show a recoverable resting state; e.g. completion does not silently generate another paid round."""

        self._run.row = None
        self._view.card.idle(title, message)
        self._view.status.setText("Practice verb forms in complete sentences · close anytime to pause")
        self._view.bar.hide()
        self._view.flag.hide()
        self._view.cancel.hide()
        self._view.options.setEnabled(True)
        self._view.options.set_active(self._run.session is not None)
        self._view.action.setText(action)
        self._view.action.setEnabled(True)
        self._view.action.setDefault(False)
        self._view.action.show()

    def _prepare(self, *, cached_first: bool = False, early: bool = False) -> None:
        """Prepare checked content off the GUI thread; e.g. generation failure preserves an earlier round."""

        if self._task or self._run.closed:
            return
        opts = self._view.options.options()
        self._db.set_setting("verb_options", opts.model_dump_json())
        if early:
            opts = opts.model_copy(update={"include_early": True})
        self._run.pending = False
        self._run.empty = ""
        self._idle("Preparing verb transformations…", "Generating and independently checking natural Dutch sentences.", "Preparing…")

        def prepare(report):
            """Return healthy empty states as data; e.g. incompatible filters are different from a network failure."""

            try:
                return self._engine.start(opts, report, cached_first=cached_first)
            except practice_service.NothingToPractice as empty:
                return empty

        self._start_task(prepare, "prepare", self._prepared)

    def _start_task(self, fn, operation: str, finished) -> None:
        """Share preparation and correction worker wiring; e.g. cancellation always leaves durable evidence intact."""

        self._run.operation = operation
        self._view.options.setEnabled(False)
        self._view.action.setEnabled(False)
        self._view.action.setDefault(False)
        self._view.flag.setEnabled(False)
        self._view.cancel.setEnabled(True)
        self._view.cancel.show()
        self._view.bar.setRange(0, 0)
        self._view.bar.show()
        task = library_workers.BackgroundTask(fn)
        self._task = task
        task.signals.progress.connect(self._view.status.setText)
        task.signals.finished.connect(finished)
        task.signals.failed.connect(self._failed)
        task.signals.cancelled.connect(self._cancelled)
        QtCore.QThreadPool.globalInstance().start(task)

    def _prepared(self, result) -> None:
        """Defer exposure until the exercise page is visible; e.g. inspecting insights during loading adds no presentation."""

        if self._run.closed:
            return
        if self._task and self._task.cancel_requested:
            self._cancelled()
            return
        self._task = None
        if isinstance(result, practice_service.NothingToPractice):
            self._run.empty = result.reason
            self._run.session = self._db.verb_active()
            action = "Resume round" if self._run.session else ("Practice early" if result.reason == "not_due" else "Try again")
            self._idle("All caught up" if result.reason == "not_due" else "Adjust your verb round", str(result), action)
            return
        self._run.session = result
        self._run.pending = True
        self._idle("Your verb round is ready", "Return to Rewrite sentences to continue.", "Continue round")
        self.activate()

    def _advance(self, *, guard: bool = False) -> None:
        """Expose one next question with a fresh input; e.g. one continuation gesture cannot submit an empty answer."""

        if not self._active or not self._run.session or self._task:
            return
        row = self._db.verb_current(self._run.session)
        if row is None:
            self._finish()
            return
        self._run.row = row
        self._run.started = time.monotonic()
        self._run.guard_until = time.monotonic() + QtWidgets.QApplication.doubleClickInterval() / 1000 if guard else 0
        self._view.card.present(row)
        summary = self._db.verb_summary(self._run.session)
        self._view.status.setText(f"Verb transformations    ·    {row['ordinal']} / {summary['planned']}    ·    {summary['correct']} correct")
        self._view.bar.setRange(0, summary["planned"])
        self._view.bar.setValue(row["ordinal"] - 1)
        self._view.bar.show()
        self._view.options.setEnabled(True)
        self._view.options.set_active(True)
        self._view.cancel.hide()
        self._view.flag.setEnabled(True)
        self._view.flag.show()
        self._view.action.setText("Retry checking" if row["answer_text"] is not None else "Check sentence")
        self._view.action.setDefault(False)
        self._view.action.show()
        self._input_changed()
        if row["answer_text"] is not None:
            self._view.card.message("Your first attempt is saved. Retry checking it to get the correction.")

    def _input_changed(self) -> None:
        """Enable checking only for bounded nonempty input; e.g. whitespace cannot become a failed attempt."""

        text = self._view.card.editor.toPlainText()
        self._view.action.setEnabled(not self._task and bool(text.strip()) and len(text) <= 2000)

    def _submit(self) -> None:
        """Persist a first attempt before starting correction; e.g. repeated Enter signals cannot duplicate activity."""

        row = self._run.row
        if not self._active or self._task or not row or row["outcome"] is not None:
            return
        if self._view.options.menu.isVisible() or time.monotonic() < self._run.guard_until:
            return
        try:
            elapsed = max(0, round((time.monotonic() - self._run.started) * 1000))
            saved = self._db.verb_submit(row["id"], self._view.card.editor.toPlainText(), elapsed)
        except (ValueError, OSError, sqlite3.Error) as exc:
            self._view.card.message(str(exc))
            return
        self._run.row = saved
        self._view.card.editor.setReadOnly(True)
        self.activity_changed.emit()
        self._view.card.message("Checking…")
        self._start_task(lambda report: self._engine.check(saved["id"], report), "grade", self._graded)

    def _graded(self, row: dict) -> None:
        """Reveal correction without advancing; e.g. a fresh click or Enter lets the learner read the explanation first."""

        if self._run.closed:
            return
        self._task = None
        self._run.row = row
        self._view.cancel.hide()
        self._view.options.setEnabled(True)
        self._view.flag.setEnabled(True)
        self._view.bar.setRange(0, self._db.verb_summary(self._run.session)["planned"])
        self._view.bar.setValue(row["ordinal"])
        if row["outcome"] == "invalid":
            self._view.card.idle("Exercise reported", "Excluded from grading and adaptive review.")
            self._view.card.arm()
            self._view.flag.setEnabled(False)
        else:
            self._view.card.feedback(row)
        self._view.status.setText("Correction saved · read it, then click Continue or press Enter")
        self._view.action.setText("Continue →")
        self._view.action.setEnabled(True)
        self._view.action.setDefault(self._active)
        if self._active:
            self._view.action.setFocus()
        self.activity_changed.emit()

    def _action(self) -> None:
        """Route the primary action from saved state; e.g. a retry grades the same immutable sentence."""

        if not self._active or self._task or self._view.options.menu.isVisible():
            return
        if self._run.row:
            if self._run.row["outcome"] is None:
                self._submit()
            else:
                self._view.card.request_continue()
        elif self._run.session:
            self._advance()
        else:
            self._prepare(early=self._run.empty == "not_due")

    def _failed(self, message: str, _trace: str) -> None:
        """Retain typed evidence after correction failures; e.g. a retry does not record a second attempt or a false failure."""

        if self._run.closed:
            return
        self._task = None
        if self._run.operation == "grade" and self._run.row:
            self._view.card.message(f"Could not check your sentence: {message}\nYour first attempt is saved; no failure was recorded.")
            self._view.action.setText("Retry checking")
            self._view.action.setEnabled(True)
            self._view.action.setDefault(False)
            self._view.options.setEnabled(True)
            self._view.flag.setEnabled(True)
            self._view.cancel.hide()
            self._view.bar.hide()
        else:
            self._run.session = self._db.verb_active()
            self._idle("Could not prepare this verb round", message, "Resume round" if self._run.session else "Try again")

    def _cancelled(self) -> None:
        """Leave cancellation paused; e.g. it must not immediately restart a paid request."""

        self._failed("Checking cancelled." if self._run.operation == "grade" else "Preparation cancelled.", "")

    def _report(self) -> None:
        """Remove defective content from every skill estimate; e.g. report a disputed AI correction after reading it."""

        if not self._run.row or self._task or not self._active:
            return
        self._db.verb_flag(self._run.row["id"])
        self._run.row["outcome"] = "invalid"
        self._view.card.idle("Exercise reported", "Excluded from grading and adaptive review. Your submitted effort stays recorded.")
        self._view.card.arm()
        self._view.flag.setEnabled(False)
        self._view.action.setText("Continue →")
        self._view.action.setEnabled(True)
        self._view.action.setDefault(True)
        self._view.action.setFocus()
        self.activity_changed.emit()

    def _stop(self) -> None:
        """Cancel a worker or explicitly finish a round; e.g. simply closing the window continues to mean pause."""

        self._view.options.menu.close()
        if self._task:
            self._task.cancel()
            self._view.cancel.setEnabled(False)
            self._view.status.setText("Cancelling after the current request…")
        elif self._run.session:
            self._finish()

    def _finish(self) -> None:
        """Show actual results without counting unseen questions as failures; e.g. a partial round keeps its history."""

        result = self._db.verb_finish(self._run.session)
        complete = result["correct"] + result["wrong"] + result["invalid"] == result["planned"]
        self._run.session = None
        self._run.pending = False
        self._run.empty = ""
        message = f"{result['correct']} correct · {result['wrong']} to revisit · {result['submitted']} submitted"
        self._idle("Verb round complete" if complete else "Verb round saved", message, "Another verb round")

    def _tab_changed(self, idx: int) -> None:
        """Keep diagnostics read-only and exposure visible; e.g. insights can open while generation runs."""

        self._view.action.setDefault(False)
        if idx == 1:
            self._progress.refresh()
        else:
            self.activate()

    def _editing(self, editing: bool) -> None:
        """Suspend dialog action keys during setup; e.g. Enter in round length cannot advance an answered sentence."""

        self._view.action.setDefault(False)
        if not editing:
            QtCore.QTimer.singleShot(0, self, self.activate)

    def show_practice(self) -> None:
        """Route an explicit Practice action back to exercises; e.g. a reminder should not leave the learner on Insights."""

        self._tabs.setCurrentIndex(0)
        self.activate()

    def showEvent(self, event) -> None:
        """Paint before automatic preparation; e.g. hidden construction does not initiate API work."""

        super().showEvent(event)
        QtCore.QTimer.singleShot(0, self, self.activate)

    def hideEvent(self, event) -> None:
        """Release dialog-default keys when hidden; e.g. Enter in vocabulary cannot advance a verb exercise."""

        self._view.action.setDefault(False)
        super().hideEvent(event)

    def shutdown(self) -> None:
        """Cancel cooperatively without awaiting HTTP; e.g. Escape closes immediately with the typed attempt durable."""

        self._run.closed = True
        if self._task:
            self._task.cancel()
