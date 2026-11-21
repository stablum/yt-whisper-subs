"""Coordinate Dutch passage practice with durable asynchronous open-answer grading.

Example: submit an explanation, continue to a choice question, then review the earlier correction from the rail.
"""

import json
import time

import pydantic as pd
from PySide6 import QtCore
from PySide6 import QtWidgets

from yt_whisper_subs import i18n
from yt_whisper_subs import comprehension as comp
from yt_whisper_subs import comprehension_service
from yt_whisper_subs import library_checks
from yt_whisper_subs import library_comprehension_view
from yt_whisper_subs import library_workers


class Run(pd.BaseModel):
    """Group transient navigation separately from immutable database answers; e.g. an arrived correction never replaces a draft being typed."""

    session: str | None = None
    row: dict | None = None
    started: float = 0
    elapsed: int = 0
    closed: bool = False
    restoring: bool = False
    phase: str = 'idle'


class Panel(QtWidgets.QWidget):
    """Bind one modality to the same audited-passage strategy; e.g. reading and listening maintain separate resumable sessions."""

    activity_changed = QtCore.Signal()
    round_reviewed = QtCore.Signal()
    speech_changed = QtCore.Signal(int, str)
    passage_requested = QtCore.Signal(list)

    def __init__(self, db, direction, *, engine=None) -> None:
        """Build without exposing content or calling a provider; e.g. opening Progress does not start a round."""

        super().__init__()
        self._db, self._direction = db, comp.Direction(direction)
        self._engine = engine or comprehension_service.ComprehensionService(db)
        self._run, self._task = Run(), None
        self.before_question = None
        self._view = library_comprehension_view.View(db, self._direction, self._engine.audio)
        self._view.source.read_requested.connect(self.passage_requested)
        self._checks = library_checks.Checks.shared(db, kind='comprehension')
        self._checks.changed.connect(self._changed)
        self._view.action.clicked.connect(self._action)
        self._view.resume.clicked.connect(self._resume)
        self._view.card.submitted.connect(self._submit)
        self._view.card.chosen.connect(self._choose)
        self._view.card.continued.connect(self._next)
        self._view.card.editor.textChanged.connect(self._input)
        self._view.rail.chosen.connect(self._jump)
        self._view.retry.clicked.connect(self._retry)
        self._view.flag.clicked.connect(self._report)
        self._view.cancel.clicked.connect(self._cancel)
        self._view.options.restart_requested.connect(lambda: self._new(cached=False))
        self._view.options.finish_requested.connect(self._finish)
        self._view.options.editing.connect(self._editing)
        self._draft = QtCore.QTimer(self)
        self._draft.setSingleShot(True)
        self._draft.setInterval(300)
        self._draft.timeout.connect(self._save_draft)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._view)
        self._view.card.idle(self._direction.label, i18n.tr('Read or listen to a Dutch text and answer the questions.'))
        for widget in (self._view.action, self._view.resume, self._view.flag, self._view.retry,
                       self._view.cancel, self._view.rail, self._view.bar):
            widget.hide()

    @property
    def round_id(self) -> str | None:
        """Identify the saved passage round for a guided checkpoint."""

        return self._run.session

    def enter_sequence(self) -> None:
        """Start cached text from an earlier summary once its corrections have been viewed."""

        if self.isVisible() and self._run.phase == 'summary':
            summary = self._db.comprehension_summary(self._run.session)
            if not summary['unseen'] and summary['graded'] + summary['invalid'] >= summary['submitted']:
                self._new(cached=True)
                return
        self.activate()

    def activate(self) -> None:
        """Open the first question automatically and restore the last review position; e.g. no Start round click is needed."""

        if self._run.closed or not self.isVisible() or self._task or self._view.options.menu.isVisible():
            return
        if self._run.row:
            self._run.started = time.monotonic()
            self._render(self._db.comprehension_get(self._run.row['id']))
            return
        if self._run.phase == 'summary':
            self._outline()
            return
        if not self._run.session:
            self._run.session = self._db.comprehension_resume(self._direction.value)
        if self._run.session:
            self._resume()
        else:
            self._new(cached=True)

    def _resume(self) -> None:
        """Prefer saved navigation, then unfinished work or unseen feedback; e.g. reopening cannot silently mark a correction viewed."""

        self._view.resume.hide()
        rows = self._db.comprehension_history(self._run.session)
        try:
            cursor = json.loads(self._db.setting(self._view.options.key + '_cursor', '{}'))
        except ValueError:
            cursor = {}
        saved = next((r for r in rows if r['id'] == cursor.get('id') and r['shown_at'] is not None), None)
        unfinished = next((r for r in rows if r['answer_text'] is None and r['outcome'] is None), None)
        unseen = next((r for r in rows if r['grade'] and r['viewed_at'] is None and r['outcome'] != 'invalid'), None)
        row = (saved or unfinished) if unfinished else (unseen or saved)
        row = row or next((r for r in rows if r['outcome'] is None), None)
        self._jump(row['id']) if row else self._summary()
        self._checks.resume(self._engine)

    def _new(self, *, cached=False) -> None:
        """Prepare privately in a worker and replace only after success; e.g. a reviewer outage leaves the previous session and draft intact."""

        if self._task or self._run.closed:
            return
        self._save_draft()
        try:
            opts = self._view.options.options()
        except ValueError as exc:
            self._view.status.setText(str(exc).split('\n')[0])
            return
        self._db.set_setting(self._view.options.key, opts.model_dump_json())
        self._run.phase = 'preparing'
        self._run.row = None
        self._view.status.clear()
        self.speech_changed.emit(0, '')
        self._view.source.clear()
        self._view.card.idle(i18n.tr('Preparing text…'), i18n.tr('The text and all questions are independently checked.'))
        self._view.action.hide()
        self._view.resume.hide()
        self._view.flag.hide()
        self._view.retry.hide()
        self._view.rail.hide()
        self._view.cancel.show()
        self._view.options.setEnabled(False)
        self._view.bar.setRange(0, 0)
        self._view.bar.show()

        def prepare(report):
            """Show quality failures separately from transport or audio failures."""

            try:
                return self._engine.start(opts, report, cached_first=cached)
            except (comp.DraftRejected, comp.AuditIncomplete) as err:
                return err

        task = library_workers.BackgroundTask(prepare)
        self._task = task
        task.signals.progress.connect(self._view.status.setText)
        task.signals.finished.connect(self._prepared)
        task.signals.failed.connect(self._failed)
        task.signals.cancelled.connect(self._cancelled)
        QtCore.QThreadPool.globalInstance().start(task)

    def _prepared(self, result) -> None:
        """Expose only when the actual practice page is visible; e.g. background completion on Activity creates no presentation."""

        self._task = None
        if self._run.closed:
            return
        self._view.options.setEnabled(True)
        self._view.cancel.hide()
        if isinstance(result, comp.AuditIncomplete):
            self._rest(i18n.tr('Preparation failed'),
                       i18n.tr('The review was incomplete. Try again to recheck the saved draft.'))
            return
        if isinstance(result, comp.DraftRejected):
            self._rest(i18n.tr('Preparation failed'),
                       i18n.tr('The text and questions did not pass the independent review. Try again for a new draft.'))
            return
        self._run.session, self._run.phase = result, 'ready'
        if self.isVisible():
            self._resume()
        else:
            self._view.card.idle(i18n.tr('Your text is ready'), i18n.tr('Return to begin.'))

    def _failed(self, _message, _trace='') -> None:
        """Keep private source/answer data out of preparation errors; e.g. a schema exception cannot leak a listening transcript."""

        self._task = None
        if not self._run.closed:
            self._rest(i18n.tr('Preparation failed'), i18n.tr('Your previous answers and prepared text are saved. Try again.'))

    def _cancelled(self) -> None:
        """Preserve cached yields after cancellation; e.g. restarting resumes an unfinished independent audit."""

        self._task = None
        if not self._run.closed:
            self._rest(i18n.tr('Preparation paused'), i18n.tr('Completed parts remain available.'))

    def _rest(self, title, message) -> None:
        """Offer a deliberate retry without starting another request; e.g. an error page never loops through paid generation."""

        self._run.phase, self._run.row = 'error', None
        self._view.status.clear()
        self.speech_changed.emit(0, '')
        self._view.card.idle(title, message)
        self._view.options.setEnabled(True)
        self._view.cancel.hide()
        self._view.bar.hide()
        self._view.rail.hide()
        self._view.flag.hide()
        self._view.retry.hide()
        self._view.resume.setVisible(bool(self._run.session))
        self._view.action.setText(i18n.tr('Try again'))
        self._view.action.setEnabled(True)
        self._view.action.show()

    def _jump(self, presentation_id: int) -> None:
        """Navigate without editing a submitted attempt; e.g. the rail can revisit question two while question five is being graded."""

        if self._task or self._run.closed:
            return
        if self.before_question and not self.before_question(self._run.session, lambda: self._jump(presentation_id)):
            return
        self._save_draft()
        row = self._db.comprehension_open(presentation_id)
        draft = self._db.comprehension_draft(presentation_id)
        self._run.elapsed, self._run.started = draft['elapsed_ms'], time.monotonic()
        self._db.set_setting(self._view.options.key + '_cursor', json.dumps(dict(id=presentation_id)))
        self._render(row, draft=draft['text'])

    def _render(self, row, *, draft=None) -> None:
        """Apply feedback gates before touching widgets; e.g. listening grades remain concealed until every answer is immutable."""

        self._run.restoring = True
        self._run.row, self._run.phase = row, 'question'
        summary = self._db.comprehension_summary(self._run.session)
        session = self._db.comprehension_session(self._run.session)
        changed = self._view.source.load(session, summary)
        if draft is None:
            draft = self._view.card.editor.toPlainText() if row['answer_text'] is None else ''
        self._view.card.present(row, draft)
        allowed = self._direction == comp.Direction.READ or summary['submitted'] == summary['planned']
        if row['grade'] and row['outcome'] != 'invalid' and allowed:
            self._view.card.feedback(row)
            if self.isVisible():
                self._db.comprehension_seen(row['id'])
                row['viewed_at'] = row['viewed_at'] or time.time()
        elif row['answer_text'] is not None:
            self._view.card.pending(row, withheld=not allowed)
        self._view.action.setText(i18n.tr('Submit answer and continue') if row['answer_text'] is None and row['kind'] == 'open' else i18n.tr('Next'))
        self._view.action.setDefault(False)
        self._view.action.show()
        self._view.retry.setVisible(row['check_state'] == 'failed' and row['outcome'] is None)
        self._view.flag.show()
        self._view.cancel.hide()
        self._view.options.setEnabled(True)
        self._view.options.set_active(session['status'] == 'active')
        self._run.restoring = False
        self._input()
        self._outline()
        if self._direction == comp.Direction.READ:
            text = row['content']['prompt'] if row['outcome'] != 'invalid' else ''
            self.speech_changed.emit(row['id'], text)
        if changed and self._direction == comp.Direction.LISTEN and summary['submitted'] == 0:
            QtCore.QTimer.singleShot(0, self, lambda: self._view.source.audio.play())

    def _outline(self) -> None:
        """Show useful completion, queue and consulted-feedback counts; e.g. finished grading replaces a stale submitted-only status."""

        if not self._run.session:
            return
        rows = self._db.comprehension_history(self._run.session)
        summary = self._db.comprehension_summary(self._run.session)
        hidden = self._direction == comp.Direction.LISTEN and summary['submitted'] < summary['planned']
        rail = []
        for row in rows:
            state = 'ready' if hidden and row['grade'] else row['check_state']
            rail.append(dict(**{k: v for k, v in row.items() if k != 'outcome'},
                             outcome=None if hidden and row['outcome'] != 'invalid' else row['outcome'], state=state, error='',
                             session_status=row['status'], feedback_ready=bool(row['grade'])))
        self._view.rail.refresh(rail, self._run.row['id'] if self._run.row else None)
        self._view.rail.show()
        self._view.bar.setRange(0, summary['planned'])
        self._view.bar.setValue(summary['submitted'])
        self._view.bar.show()
        if summary['graded'] == summary['planned'] - summary['invalid']:
            score = i18n.number(summary['score'], '.0%') if summary['score'] is not None else '—'
            message = i18n.tr(
                'Comprehension {score} · {correct}/{graded} fully correct · {unseen} unseen corrections',
                score=score,
                correct=summary['correct'],
                graded=summary['graded'],
                unseen=summary['unseen'],
            )
        else:
            message = i18n.tr(
                '{submitted}/{planned} submitted · {value} pending · {unseen} unseen corrections',
                submitted=summary['submitted'],
                planned=summary['planned'],
                value=summary['submitted'] - summary['graded'] - summary['invalid'],
                unseen=summary['unseen'],
            )
        self._view.status.setText(message)

    def _input(self) -> None:
        """Autosave real input without submitting it; e.g. whitespace cannot become a failed open answer."""

        if self._run.restoring or not self._run.row:
            return
        row = self._run.row
        text = self._view.card.editor.toPlainText()
        unanswered = row['answer_text'] is None
        self._view.action.setEnabled(not unanswered or row['kind'] == 'open' and bool(text.strip()) and len(text.strip()) <= 4000)
        if unanswered and row['kind'] == 'open':
            self._draft.start()

    def _save_draft(self) -> None:
        """Accumulate active editing time only; e.g. minutes spent browsing tracking are excluded."""

        self._draft.stop()
        row = self._run.row
        if row and row['answer_text'] is None:
            now = time.monotonic()
            self._run.elapsed += round((now - self._run.started) * 1000) if self._run.started else 0
            self._run.started = now
            if row['kind'] == 'open':
                self._db.comprehension_save_draft(row['id'], self._view.card.editor.toPlainText(), self._run.elapsed)

    def _choose(self, idx) -> None:
        """Make choosing and continuing separate gestures; e.g. a double-click cannot answer the next question."""

        if not self._run.row:
            return
        if self._run.row['answer_text'] is not None:
            self._view.card.request_continue()
        else:
            with self._view.card.choices.checking(idx):
                self._submit(idx)

    def _submit(self, selected=None) -> None:
        """Persist first, then continue while semantic grading runs; e.g. an API timeout never loses typed Dutch."""

        row = self._run.row
        if self._task or row is None or row['answer_text'] is not None or self._view.options.menu.isVisible():
            return
        self._save_draft()
        answer = selected if row['kind'] == 'mc' else self._view.card.editor.toPlainText()
        try:
            updated = self._db.comprehension_submit(row['id'], answer, self._run.elapsed)
        except ValueError:
            return
        self._run.row = updated
        self.activity_changed.emit()
        self._checks.resume(self._engine)
        summary = self._db.comprehension_summary(self._run.session)
        if row['kind'] == 'open' or summary['submitted'] == summary['planned']:
            self._next()
        else:
            self._render(updated)

    def _action(self) -> None:
        """Use one explicit primary action; e.g. Enter submits only a nonempty open response, while feedback advances."""

        if self._run.row and self._run.row['answer_text'] is None:
            self._submit()
        else:
            self._next()

    def _next(self) -> None:
        """Walk reviewed answers in order, then find unseen feedback; e.g. clicking an old correction does not jump straight to the end screen."""

        if self._task or self._run.closed or not self.isVisible():
            return
        if self._run.phase == 'error':
            self._new(cached=False)
            return
        if self._run.row is None:
            self._new(cached=True)
            return
        rows = self._db.comprehension_history(self._run.session)
        current = self._run.row
        unanswered = next((r for r in rows if r['answer_text'] is None and r['outcome'] is None), None)
        if unanswered:
            following = next((r for r in rows if r['ordinal'] > current['ordinal'] and (r['shown_at'] is not None or r['id'] == unanswered['id'])), unanswered)
            self._jump(following['id'])
            return
        if current['grade'] and current['viewed_at'] is not None:
            following = next((r for r in rows if r['ordinal'] > current['ordinal']), None)
            if following:
                self._jump(following['id'])
                return
        following = next((r for r in rows if r['ordinal'] > current['ordinal'] and r['grade'] and r['viewed_at'] is None and r['outcome'] != 'invalid'), None)
        following = following or next((r for r in rows if r['grade'] and r['viewed_at'] is None and r['outcome'] != 'invalid'), None)
        following = following or next((r for r in rows if r['outcome'] is None), None)
        self._jump(following['id']) if following else self._summary()

    def _summary(self) -> None:
        """Retain the rail and source for review without generating another round; e.g. a finished passage is still inspectable."""

        self._run.row, self._run.phase = None, 'summary'
        self.speech_changed.emit(0, '')
        result = self._db.comprehension_summary(self._run.session)
        score = i18n.number(result['score'], '.0%') if result['score'] is not None else '—'
        self._view.card.idle(i18n.tr('Round complete') if not result['unseen'] and result['graded'] == result['submitted'] else i18n.tr('Your answers are saved'),
                             i18n.tr(
                                 'Comprehension {score} · {correct} fully correct. Choose an earlier question on the left '
                                 'or start a new text.',
                                 score=score,
                                 correct=result['correct'],
                             ))
        self._view.action.setText(i18n.tr('New text'))
        self._view.action.setEnabled(True)
        self._view.action.show()
        self._view.retry.hide()
        self._view.source.load(self._db.comprehension_session(self._run.session), result)
        self._outline()
        if result['planned'] and result['graded'] + result['invalid'] == result['planned'] and not result['unseen']:
            self.round_reviewed.emit()

    def _changed(self, presentation_id) -> None:
        """Refresh only relevant visible feedback; e.g. another answer's correction never replaces an unfinished draft."""

        if self._run.closed or not self._run.session or self._task:
            return
        rows = self._db.comprehension_history(self._run.session)
        if not any(r['id'] == presentation_id for r in rows):
            return
        self.activity_changed.emit()
        if not self.isVisible():
            return
        if self._run.row and self._run.row['id'] == presentation_id:
            self._render(self._db.comprehension_get(presentation_id))
        elif all(r['answer_text'] is not None for r in rows) and (self._run.row is None or self._run.row['grade'] is None):
            ready = next((r for r in rows if r['grade'] and r['viewed_at'] is None and r['outcome'] != 'invalid'), None)
            if ready:
                self._jump(ready['id'])
        self._outline()

    def _retry(self) -> None:
        """Retry a failed correction explicitly; e.g. pending work is not repeatedly charged in the background."""

        if self._run.row:
            self._checks.retry(self._run.row['id'], self._engine)

    def _report(self) -> None:
        """Quarantine faulty source reasoning with an actual user report; e.g. a disputed answer stops contributing word failures."""

        if not self._run.session:
            return
        reason, accepted = QtWidgets.QInputDialog.getText(self, i18n.tr('Report text or question'), i18n.tr('What is wrong?'))
        if accepted and reason.strip():
            session = self._db.comprehension_session(self._run.session)
            self._db.comprehension_flag(session['passage_id'], reason.strip())
            self._run.session = None
            self._view.source.clear()
            self._rest(i18n.tr('Text reported'), i18n.tr('Your submitted answers remain in your activity but no longer count toward scores.'))
            self.activity_changed.emit()

    def _cancel(self) -> None:
        """Cancel only owned preparation; e.g. already submitted corrections remain in their application-owned queue."""

        if self._task:
            self._task.cancel()

    def _finish(self) -> None:
        """End an active round without discarding answered history; e.g. closing the window remains the gentler pause operation."""

        self._save_draft()
        if self._run.session:
            self._db.comprehension_finish(self._run.session)
            self._summary()

    def _editing(self, editing) -> None:
        """Suspend question shortcuts during configuration; e.g. a digit in a count field never selects an answer."""

        self._view.card.choices.shortcuts(not editing and bool(self._run.row) and self._run.row['answer_text'] is None and self._run.row['kind'] == 'mc')

    def showEvent(self, event) -> None:
        """Paint before automatic activation; e.g. hidden construction cannot expose a prepared question."""

        super().showEvent(event)
        QtCore.QTimer.singleShot(0, self, self.activate)

    def hideEvent(self, event) -> None:
        """Preserve the current draft and stop narration on navigation; e.g. time on Progress is not answer-writing time."""

        self._save_draft()
        self._run.started = 0
        self._view.source.audio.stop()
        self._view.card.choices.shortcuts(False)
        self._view.action.setDefault(False)
        super().hideEvent(event)

    def shutdown(self) -> None:
        """Leave durable grades running while releasing this view; e.g. reopening restores unsent text and unseen corrections."""

        self._save_draft()
        self._run.closed = True
        self._view.source.audio.shutdown()
        if self._task:
            self._task.cancel()
