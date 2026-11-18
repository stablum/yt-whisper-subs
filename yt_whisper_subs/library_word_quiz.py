"""One resumable, direction-bound word quiz with guarded native interaction.

Example: switching to English-to-Dutch leaves the Dutch-to-English question and choices intact.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime

from PySide6 import QtCore
from PySide6 import QtWidgets

from yt_whisper_subs import i18n
from yt_whisper_subs import library_practice_controls as controls
from yt_whisper_subs import library_workers
from yt_whisper_subs import practice
from yt_whisper_subs import practice_round
from yt_whisper_subs import practice_service
from yt_whisper_subs import practice_tracks


class WordQuiz(QtWidgets.QWidget):
    """Own one direction's view and lifecycle; e.g. independent preparation cannot overwrite the other direction's current answer."""

    activity_changed = QtCore.Signal()
    round_reviewed = QtCore.Signal()

    def __init__(self, db, direction: practice.Direction, *, engine=None) -> None:
        """Restore only this destination's session; e.g. hidden construction never records exposure."""

        super().__init__()
        self._db, self._direction = db, direction
        self._track = practice_tracks.Track.for_options("practice", practice.RoundOptions(direction=direction, count=10))
        self._engine = engine or practice_service.PracticeService(db)
        self._session = db.practice_active(direction.value)
        self._current = self._task = self._empty = None
        self._started = 0.0
        self._autostart_pending = True
        self._at_end = False
        self._closed = False
        self._arrange()
        self._controls.restore(db.setting(self._track.settings_key, db.setting("practice_options", "")))
        self._idle(i18n.tr('Getting your next word ready…'), "", i18n.tr('Continue'))
        self._next.hide()

    @property
    def round_id(self) -> str | None:
        """Identify the round whose answers advance the guided session."""

        return self._session

    def enter_sequence(self) -> None:
        """Start a fresh cached round when returning to a result screen from earlier practice."""

        if self._at_end and self._practicing:
            self._prepare(cached_first=True)
        else:
            self.activate()

    def activate(self) -> None:
        """Restore keys only for the visible direction; e.g. number shortcuts on a hidden quiz cannot answer its question."""

        if not self._practicing:
            return
        question = self._current is not None and self._current["outcome"] is None
        self._answers.shortcuts(question and not self._controls.menu.isVisible())
        self._next.setDefault(self._next.isEnabled())
        self._ensure_round()

    def hideEvent(self, event) -> None:
        """Disable hidden-page actions; e.g. Enter and number keys belong to the selected top-level destination."""

        self._answers.shortcuts(False)
        self._next.setDefault(False)
        super().hideEvent(event)

    def shutdown(self) -> None:
        """Pause without changing history and cancel owned work; e.g. closing practice preserves both directions."""

        self._closed = True
        if self._task:
            self._task.cancel()

    def _arrange(self) -> None:
        """Keep the question and continuation in stable positions; e.g. occasional setup lives in the options popover."""

        self._controls = controls.RoundControls(self._direction)
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
        self._unsure = QtWidgets.QCheckBox(i18n.tr("I'm unsure / guessing"))
        unsure_size = self._unsure.sizePolicy()
        unsure_size.setRetainSizeWhenHidden(True)
        self._unsure.setSizePolicy(unsure_size)
        self._unsure.setToolTip(i18n.tr('A correct guess gets a shorter review interval. Response speed does not affect grading.'))
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
        self._details.setText(i18n.tr('Why this answer?'))
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
        self._flag = QtWidgets.QPushButton(i18n.tr('Report question'))
        self._flag.setAutoDefault(False)
        self._cancel = QtWidgets.QPushButton(i18n.tr('Cancel preparation'))
        self._cancel.setAutoDefault(False)
        footer = QtWidgets.QHBoxLayout()
        footer.addWidget(self._flag)
        footer.addWidget(self._cancel)
        footer.addStretch()
        footer.addWidget(self._next)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._surface)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(12)
        layout.addLayout(heading)
        layout.addWidget(self._bar)
        layout.addWidget(scroll, 1)
        layout.addLayout(footer)
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
        self._status.setText(i18n.tr('Your answers are saved as you go.'))
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

        return not self._closed and self.isVisible()

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
        self._db.set_setting(self._track.settings_key, opts.model_dump_json())
        if early:
            opts = opts.model_copy(update={"include_early": True})
        self._autostart_pending = False
        self._empty = None
        self._at_end = False
        self._idle(i18n.tr('Preparing your round…'), i18n.tr('Getting the choices ready.'), i18n.tr('Preparing…'))
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
            self._session = self._db.practice_active(self._direction.value)
            titles = {"not_due": i18n.tr("You're all caught up"), "no_words": i18n.tr('No words ready yet'),
                      "not_enough": i18n.tr('Choose a smaller round'), "no_questions": i18n.tr('Could not prepare this round')}
            action = i18n.tr('Practice early') if result.reason == "not_due" else i18n.tr('Try again')
            self._idle(titles[result.reason], str(result), i18n.tr('Resume round') if self._session else action)
            return
        self._session = result
        self._autostart_pending = True
        self._idle(i18n.tr('Your round is ready'), i18n.tr('Return to Word quiz to continue.'), i18n.tr('Continue round'))
        self._ensure_round()

    @QtCore.Slot(str, str)
    def _failed(self, message: str, _trace: str) -> None:
        """Offer an inline recovery while retaining the previous round; e.g. retry a failed generation request."""

        if not self._closed:
            self._session = self._db.practice_active(self._direction.value)
            self._idle(i18n.tr("Couldn't prepare this round"), message, i18n.tr('Resume round') if self._session else i18n.tr('Try again'))

    @QtCore.Slot()
    def _cancelled(self) -> None:
        """Leave a cancelled preparation paused rather than immediately restarting; e.g. keep a prior round resumable."""

        if not self._closed:
            self._session = self._db.practice_active(self._direction.value)
            self._idle(i18n.tr('Practice paused'), i18n.tr('Your progress is saved.'), i18n.tr('Resume round') if self._session else i18n.tr('Try again'))

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
        self._status.setText(i18n.tr(
            '{label}    ·    {ordinal} / {planned}    ·    {correct} correct',
            label=direction.label,
            ordinal=row['ordinal'],
            planned=summary['planned'],
            correct=summary['correct'],
        ))
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
        self._next.setText(i18n.tr('Choose an answer'))
        self._next.setEnabled(False)
        self._next.setDefault(False)
        self._next.show()
        self._cancel.hide()
        self._flag.setEnabled(True)
        self._flag.show()
        self._hint.setText(i18n.tr('1 / 2 / 3 to answer · Close anytime to pause'))
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
            with self._answers.checking(idx):
                row = self._db.practice_answer(self._current["id"], idx, self._unsure.isChecked(), elapsed)
        except (ValueError, OSError, sqlite3.Error) as exc:
            self._result.setText(i18n.tr('Could not save your answer: {exc}', exc=exc))
            return
        self._current = row
        if row["outcome"] == "invalid":
            self._report()
            return
        self._answers.reveal(row["correct_idx"], idx)
        self._unsure.hide()
        verdict = i18n.tr('Correct') if row["outcome"] == "correct" else i18n.tr('Not quite')
        answer = row["content"]["answer"]
        self._result.setText(f"{verdict} · {answer}")
        self._result.setStyleSheet("color: #9ae7b4;" if row["outcome"] == "correct" else "color: #ffd166;")
        explanation = row["content"]["explanation"]
        if row["outcome"] == "wrong":
            explanation += "\n" + row["choices"][idx]["explanation"]
        due = i18n.date_text(datetime.fromtimestamp(row['due_at']), '%d %b, %H:%M')
        self._explanation.setText(i18n.tr('{explanation}\nNext review: {due}', explanation=explanation, due=due))
        self._details.show()
        self._bar.setValue(row["ordinal"])
        self._offer_continue()
        self.activity_changed.emit()

    def _offer_continue(self) -> None:
        """Give mouse and keyboard the same deliberate next action; e.g. Space or Enter advances after feedback."""

        self._surface.arm()
        target = i18n.tr('Click the card or an answer again') if self._answers.isVisible() else i18n.tr('Click the card')
        self._hint.setText(i18n.tr('{target} · Space / Enter to continue', target=target))
        self._next.setText(i18n.tr('Continue →'))
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
            QtCore.QTimer.singleShot(0, self, self.activate)

    def _report(self) -> None:
        """Keep reporting useful before or after answering; e.g. a quarantined question can be passed with one next click."""

        if self._current and self._flag.isEnabled():
            self._db.practice_flag(self._current["id"])
            self._current["outcome"] = "invalid"
            self.activity_changed.emit()
            self._result.setText(i18n.tr('Question reported · excluded from grading'))
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
            self._status.setText(i18n.tr('Cancelling after the current request…'))
        elif self._session:
            self._finish(complete=False)

    def _finish(self, *, complete: bool) -> None:
        """Show one concise completion card with the next action; e.g. move straight into another configured round."""

        result = self._db.practice_finish(self._session)
        complete = complete or result["correct"] + result["wrong"] + result["invalid"] == result["planned"]
        self._session = None
        self._empty = None
        counts = i18n.tr('{correct} correct · {wrong} to revisit', correct=result['correct'], wrong=result['wrong'])
        if result["invalid"]:
            counts += i18n.tr(' · {invalid} reported', invalid=result['invalid'])
        self._idle(i18n.tr('Round complete') if complete else i18n.tr('Round saved'), counts, i18n.tr('Another round'))
        self._at_end = True
        if complete:
            self.round_reviewed.emit()

    def showEvent(self, event) -> None:
        """Let the window paint before preparing; e.g. no blocking or exposure during construction."""

        super().showEvent(event)
        QtCore.QTimer.singleShot(0, self, self.activate)
