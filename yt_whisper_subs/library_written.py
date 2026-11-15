"""Shared native lifecycle for durable typed practice, using injected domain operations.

Example: verbs and full-sentence translation share deliberate submit/feedback/continue behavior.
"""

from __future__ import annotations

import sqlite3
import time
import functools
from collections import abc

import pydantic as pd
from PySide6 import QtCore
from PySide6 import QtWidgets

from yt_whisper_subs import practice_round
from yt_whisper_subs import library_workers


class SessionOps(pd.BaseModel):
    """Inject a coherent persistence strategy; e.g. sentence and verb transactions retain their own domain rules."""

    model_config = pd.ConfigDict(arbitrary_types_allowed=True)
    active: abc.Callable
    current: abc.Callable
    submit: abc.Callable
    summary: abc.Callable
    flag: abc.Callable
    finish: abc.Callable

    @classmethod
    def for_db(cls, db, kind: str, direction: str | None = None) -> SessionOps:
        """Bind the explicit operation family once; e.g. changing a mode cannot accidentally submit into another history."""

        operations = {name: getattr(db, f"{kind}_{name}") for name in cls.model_fields}
        if direction:
            operations["active"] = functools.partial(operations["active"], direction)
        return cls(**operations)


class Mode(pd.BaseModel):
    """Group the domain's identity, native view and transactional strategy; e.g. shared flow never formats grammar-specific prompts."""

    model_config = pd.ConfigDict(arbitrary_types_allowed=True)
    key: str
    settings_key: str
    label: str
    tab_name: str
    intro: str
    view: object
    ops: SessionOps


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



class WrittenPanel(QtWidgets.QWidget):
    """Coordinate durable typed practice; e.g. verbs and translations share separate correction/continue gestures."""

    activity_changed = QtCore.Signal()

    def __init__(self, db, mode: Mode, *, engine) -> None:
        """Construct without exposure or provider calls; e.g. statistics can open without starting a paid round."""

        super().__init__()
        self._db, self._mode, self._engine = db, mode, engine
        self._run = _Run(session=mode.ops.active())
        self._task: library_workers.BackgroundTask | None = None
        self._view = mode.view
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._view)
        self._view.action.clicked.connect(self._action)
        self._view.card.submitted.connect(self._submit)
        self._view.card.editor.textChanged.connect(self._input_changed)
        self._view.card.continued.connect(lambda: self._advance(guard=True))
        self._view.flag.clicked.connect(self._report)
        self._view.cancel.clicked.connect(self._stop)
        self._view.options.restart_requested.connect(self._prepare)
        self._view.options.finish_requested.connect(self._stop)
        self._view.options.editing.connect(self._editing)
        self._idle(mode.label, mode.intro, "Continue")
        self._view.action.hide()

    @property
    def _active(self) -> bool:
        """Protect against hidden question exposure; e.g. preparation may finish while viewing the calendar."""

        return not self._run.closed and self.isVisible()

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
        self._view.status.setText(f"{self._mode.label} · close anytime to pause")
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
        self._db.set_setting(self._mode.settings_key, opts.model_dump_json())
        if early:
            opts = opts.model_copy(update={"include_early": True})
        self._run.pending = False
        self._run.empty = ""
        self._idle(f"Preparing {self._mode.label.lower()}…", "Getting natural sentences ready and checking them.", "Preparing…")

        def prepare(report):
            """Return healthy empty states as data; e.g. incompatible filters are different from a network failure."""

            try:
                return self._engine.start(opts, report, cached_first=cached_first)
            except practice_round.NothingToPractice as empty:
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
        if isinstance(result, practice_round.NothingToPractice):
            self._run.empty = result.reason
            self._run.session = self._mode.ops.active()
            action = "Resume round" if self._run.session else ("Practice early" if result.reason == "not_due" else "Try again")
            self._idle("All caught up" if result.reason == "not_due" else "Adjust your round", str(result), action)
            return
        self._run.session = result
        self._run.pending = True
        self._idle("Your round is ready", f"Return to {self._mode.tab_name} to continue.", "Continue round")
        self.activate()

    def _advance(self, *, guard: bool = False) -> None:
        """Expose one next question with a fresh input; e.g. one continuation gesture cannot submit an empty answer."""

        if not self._active or not self._run.session or self._task:
            return
        row = self._mode.ops.current(self._run.session)
        if row is None:
            self._finish()
            return
        self._present(row, guard=guard)

    def _present(self, row: dict, *, guard: bool = False) -> None:
        """Render a selected saved exercise consistently; e.g. a sentence history view can reuse normal question and footer controls."""

        self._run.row = row
        self._run.started = time.monotonic()
        self._run.guard_until = time.monotonic() + QtWidgets.QApplication.doubleClickInterval() / 1000 if guard else 0
        self._view.card.present(row)
        summary = self._mode.ops.summary(self._run.session)
        self._view.status.setText(f"{self._mode.label}    ·    {row['ordinal']} / {summary['planned']}    ·    {summary['correct']} correct")
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
            saved = self._mode.ops.submit(row["id"], self._view.card.editor.toPlainText(), elapsed)
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
        self._view.bar.setRange(0, self._mode.ops.summary(self._run.session)["planned"])
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
            self._run.session = self._mode.ops.active()
            self._idle("Could not prepare this round", message, "Resume round" if self._run.session else "Try again")

    def _cancelled(self) -> None:
        """Leave cancellation paused; e.g. it must not immediately restart a paid request."""

        self._failed("Checking cancelled." if self._run.operation == "grade" else "Preparation cancelled.", "")

    def _report(self) -> None:
        """Remove defective content from every skill estimate; e.g. report a disputed AI correction after reading it."""

        if not self._run.row or self._task or not self._active:
            return
        self._mode.ops.flag(self._run.row["id"])
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

        result = self._mode.ops.finish(self._run.session)
        complete = result["correct"] + result["wrong"] + result["invalid"] == result["planned"]
        self._run.session = None
        self._run.pending = False
        self._run.empty = ""
        message = f"{result['correct']} correct · {result['wrong']} to revisit · {result['submitted']} submitted"
        self._idle(f"{self._mode.key.capitalize()} round complete" if complete else f"{self._mode.key.capitalize()} round saved", message, f"Another {self._mode.key} round")

    def _editing(self, editing: bool) -> None:
        """Suspend dialog action keys during setup; e.g. Enter in round length cannot advance an answered sentence."""

        self._view.action.setDefault(False)
        if not editing:
            QtCore.QTimer.singleShot(0, self, self.activate)

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
