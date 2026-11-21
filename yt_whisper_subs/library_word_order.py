"""Present saved sentence builder rounds and immediate native visual feedback."""

import json
import time

from PySide6 import QtCore, QtWidgets

from yt_whisper_subs import i18n
from yt_whisper_subs import library_order_blocks as blocks
from yt_whisper_subs import library_order_replay
from yt_whisper_subs import library_workers
from yt_whisper_subs import practice_tracks
from yt_whisper_subs import word_order
from yt_whisper_subs import word_order_service


class Panel(QtWidgets.QWidget):
    """Keep a draft and its feedback tied to a visible question, e.g. tab changes cannot submit it."""

    activity_changed = QtCore.Signal()
    round_reviewed = QtCore.Signal()
    speech_changed = QtCore.Signal(int, str)
    def __init__(self, db, *, variant=word_order.Variant.WORDS, engine=None) -> None:
        """Build the native board, feedback and compact round options."""

        super().__init__()
        self._db = db
        self._variant = word_order.Variant(variant)
        track = practice_tracks.Track.for_options("word_order", word_order.Options(variant=self._variant))
        self._key = track.settings_key
        self._engine = engine or word_order_service.Service(db)
        self._round, self._row, self._task = None, None, None
        self._started, self._finished, self._closed = 0.0, False, False
        self.before_question = None
        self._counter = QtWidgets.QLabel()
        self._options = self._round_options()
        self._prompt = QtWidgets.QLabel()
        self._prompt.setObjectName("practicePrompt")
        self._hint = QtWidgets.QLabel()
        self._moves = library_order_replay.Correction()
        self._board = blocks.Board(self._moves)
        self._result = QtWidgets.QLabel()
        self._result.setObjectName("practiceResult")
        self._answer = QtWidgets.QLabel()
        self._answer.setStyleSheet("font-size:18pt;font-weight:600;color:#8ee2af;")
        self._note = QtWidgets.QLabel()
        for label in (self._prompt, self._hint, self._result, self._answer, self._note):
            label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
            label.setWordWrap(True)
        self._board.changed.connect(self._draft)
        self._check = self._button(i18n.tr('Check answer'), self._submit, primary=True)
        self._next = self._button(i18n.tr('Next question'), self._advance, primary=True)
        self._reset = self._button(i18n.tr('Clear sentence'), self._board.clear_answer)
        self._replay = self._button(i18n.tr('Replay moves'), self._replay_moves)
        self._report = self._button(i18n.tr('Report exercise'), self._flag)
        self._again = self._button(i18n.tr('New round'), self._new, primary=True)
        self._cancel = self._button(i18n.tr('Cancel preparation'), self._cancel_task)
        self._lay_out()
        self._idle(self._variant.label, '')

    @property
    def round_id(self) -> str | None:
        """Expose the current round to guided practice and pronunciation."""

        return self._round

    def _button(self, text, handler, *, primary=False):
        """Keep footer actions deliberate, e.g. Space on a tile never activates Check."""

        button = QtWidgets.QPushButton(text)
        button.setAutoDefault(False)
        if primary:
            button.setObjectName("primaryButton")
        button.clicked.connect(handler)
        return button

    def _round_options(self):
        """Apply extra-block counts and length only to a new round."""

        try:
            opts = word_order.Options.model_validate_json(self._db.setting(self._key, '{}'))
        except ValueError:
            opts = word_order.Options()
        self._count, self._extras = QtWidgets.QSpinBox(), QtWidgets.QSpinBox()
        self._count.setRange(1, 30)
        self._extras.setRange(0, 4)
        self._count.setValue(opts.count)
        self._extras.setValue(opts.extras)
        self._early = QtWidgets.QCheckBox(i18n.tr('Include words not due yet'))
        self._early.setChecked(opts.include_early)
        options = QtWidgets.QToolButton()
        options.setObjectName("practiceOptions")
        options.setText(i18n.tr('Round options'))
        options.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QtWidgets.QMenu(options)
        panel = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(panel)
        form.addRow(i18n.tr('Questions per round'), self._count)
        form.addRow(i18n.tr('Extra blocks per sentence'), self._extras)
        form.addRow(self._early)
        start = self._button(i18n.tr('Prepare new round'), self._new)
        start.clicked.connect(menu.close)
        form.addRow(start)
        fields = QtWidgets.QWidgetAction(menu)
        fields.setDefaultWidget(panel)
        menu.addAction(fields)
        options.setMenu(menu)
        return options

    def _lay_out(self) -> None:
        """Keep the action visible while a long correction can scroll independently."""

        layout = QtWidgets.QVBoxLayout(self)
        header = QtWidgets.QHBoxLayout()
        header.addWidget(self._counter, 1)
        header.addWidget(self._options)
        layout.addLayout(header)
        self._scroll = QtWidgets.QScrollArea()
        self._scroll.setWidgetResizable(True)
        content = QtWidgets.QWidget()
        body = QtWidgets.QVBoxLayout(content)
        for widget in (self._prompt, self._hint, self._board, self._result, self._answer, self._note):
            body.addWidget(widget)
        body.addStretch()
        self._scroll.setWidget(content)
        layout.addWidget(self._scroll, 1)
        footer = QtWidgets.QHBoxLayout()
        for widget in (self._report, self._reset, self._replay):
            footer.addWidget(widget)
        footer.addStretch()
        for widget in (self._cancel, self._again, self._check, self._next):
            footer.addWidget(widget)
        layout.addLayout(footer)

    def activate(self) -> None:
        """Open the saved round or take prepared stock as soon as the mode becomes visible."""

        if self._closed or self._task or self._finished or not self.isVisible():
            return
        self._round = self._round or self._db.word_order_resume(self._variant)
        if self._round:
            self._open_question()
        else:
            self._new()

    def enter_sequence(self) -> None:
        """Start fresh after an earlier completed round when entering Full Practice."""

        if self._finished:
            self._new()
        else:
            self.activate()

    def _new(self) -> None:
        """Load a matching spare through the normal worker without replacing a saved round on failure."""

        if self._closed or self._task:
            return
        opts = word_order.Options(count=self._count.value(), extras=self._extras.value(),
                                 include_early=self._early.isChecked(), variant=self._variant)
        self._db.set_setting(self._key, opts.model_dump_json())
        self._idle(i18n.tr('Preparing Dutch sentence builds…'), '')
        self._again.hide()
        self._cancel.show()
        self._options.setEnabled(False)
        task = library_workers.BackgroundTask(lambda report: self._engine.start(opts, report))
        self._task = task
        task.signals.progress.connect(self._hint.setText)
        task.signals.finished.connect(self._prepared)
        task.signals.failed.connect(self._failed)
        task.signals.cancelled.connect(self._paused)
        QtCore.QThreadPool.globalInstance().start(task)

    def _prepared(self, session_id: str) -> None:
        """Keep prepared questions unexposed if navigation already moved elsewhere."""

        self._task = None
        if self._closed:
            return
        self._round, self._finished = session_id, False
        self._options.setEnabled(True)
        self._cancel.hide()
        if self.isVisible():
            self._open_question()

    def _failed(self, message: str, _trace: str) -> None:
        """Explain how to recover while keeping any earlier draft resumable."""

        self._task = None
        if self._closed:
            return
        self._options.setEnabled(True)
        self._idle(i18n.tr('Could not prepare this round'), message.partition('\n')[0])
        self._restore_action()

    def _paused(self) -> None:
        """Let cancellation stop the wait without discarding checked content."""

        self._failed(i18n.tr('Completed parts remain available.'), '')

    def _cancel_task(self) -> None:
        """Cancel the panel's current worker cooperatively."""

        if self._task:
            self._task.cancel()

    def _restore_action(self) -> None:
        """Offer previous work after failed or cancelled preparation."""

        if self._round and not self._finished:
            self._next.setText(i18n.tr('Resume previous round'))
            self._next.show()

    def _open_question(self) -> None:
        """Render an existing draft or unacknowledged feedback from the catalog."""

        if self.before_question and not self.before_question(self._round, self.activate):
            self._idle(i18n.tr('Preparing Dutch speech…'), '')
            self._again.hide()
            return
        self._row = self._db.word_order_current(self._round)
        summary = self._db.word_order_summary(self._round)
        self._counter.setText(i18n.tr('{answered} / {total} answered', answered=summary["submitted"], total=summary["planned"]))
        if self._row is None:
            self._finished = True
            self._idle(i18n.tr('Round complete'), i18n.tr('{correct} correct out of {total}',
                                                         correct=summary["correct"], total=summary["submitted"]))
            self.round_reviewed.emit()
            return
        draft = word_order.Draft.model_validate_json(self._row["body_json"])
        selected = json.loads(self._row["answer_json"] or self._row["draft_json"])
        self._idle(draft.english, i18n.tr('Click blocks to build the Dutch sentence. Drag to rearrange or click to return a block.'))
        self._again.hide()
        self._report.show()
        self._board.open(draft.bank, json.loads(self._row["tray_json"]), selected)
        self._board.show()
        if self._row["outcome"]:
            self._feedback(draft, selected)
        else:
            self._started = time.monotonic()
            self._reset.show()
            self._check.setEnabled(bool(selected))
            self._check.show()

    def _draft(self, selected: list[int]) -> None:
        """Save each click and drag immediately so unfinished builds survive restart."""

        if self._row and not self._row["outcome"]:
            self._db.word_order_draft(self._row["id"], selected)
            self._check.setEnabled(bool(selected))

    def _submit(self) -> None:
        """Grade on the spot using the audited solutions."""

        if not self._row or self._row["outcome"] or not self._board.selected:
            return
        try:
            self._row = self._db.word_order_answer(self._row["id"], self._board.selected, int((time.monotonic() - self._started) * 1000))
        except ValueError as exc:
            self._hint.setText(str(exc))
            return
        self.activity_changed.emit()
        self._open_question()

    def _feedback(self, draft: word_order.Draft, selected: list[int]) -> None:
        """Explain the rule, exclusions and omissions while replaying the block movement."""

        grade = word_order.Grade.model_validate_json(self._row["grade_json"])
        self._prompt.setStyleSheet("font-size:18pt;")
        self._hint.setText(i18n.tr(word_order.PATTERNS[draft.pattern]))
        self._result.setText(i18n.tr('Correct order') if grade.correct else i18n.tr('Order score: {score:.0%}', score=grade.partial))
        self._result.show()
        self._board.reveal(None if grade.correct else grade.solution)
        if not grade.correct:
            self._replay.show()
        self._answer.setText(draft.dutch)
        self._answer.show()
        notes = [draft.explanation]
        if grade.solution != list(range(len(draft.tiles))):
            notes.append(i18n.tr('This order also works.') if grade.correct else
                         i18n.tr('The replay uses the valid order closest to your submission.'))
        if grade.omitted:
            notes.append(i18n.tr('Missing blocks: {blocks}', blocks=' · '.join(draft.bank[idx] for idx in grade.omitted)))
        for idx in grade.excluded:
            notes.append(i18n.tr('Leave out “{block}”. {reason}', block=draft.bank[idx], reason=draft.extras[idx - len(draft.tiles)].why))
        self._note.setText('\n\n'.join(notes))
        self._note.show()
        self._next.setText(i18n.tr('Next question'))
        self._next.show()
        self.speech_changed.emit(self._row["id"], draft.dutch)

    def _advance(self) -> None:
        """Acknowledge feedback and advance, or resume if preparing a replacement failed."""

        if self._row and self._row["outcome"] and self._result.isVisible():
            self._db.word_order_viewed(self._row["id"])
        self._open_question()

    def _flag(self) -> None:
        """Exclude a disputed exercise and continue with the next checked build."""

        if self._row:
            self._db.word_order_report(self._row["id"])
            self._open_question()

    def _replay_moves(self) -> None:
        """Bring the correction into view before replaying the arrows."""

        self._scroll.ensureWidgetVisible(self._moves, 0, 4)
        self._moves.replay()

    def _idle(self, title: str, hint: str) -> None:
        """Reset visible controls without touching the saved round or the draft."""

        self.speech_changed.emit(0, "")
        self._prompt.setStyleSheet("")
        self._prompt.setText(title)
        self._hint.setText(hint)
        self._scroll.verticalScrollBar().setValue(0)
        self._again.show()
        for widget in (self._board, self._result, self._answer, self._note, self._check, self._reset,
                       self._replay, self._report, self._next, self._cancel):
            widget.hide()

    def showEvent(self, event) -> None:
        """Resume the last-used ordering page when the Practice window opens."""

        super().showEvent(event)
        QtCore.QTimer.singleShot(0, self, self.activate)

    def shutdown(self) -> None:
        """Cancel panel-only work while retaining all saved blocks and answers."""

        self._closed = True
        self._cancel_task()
