"""Navigate written exercises while durable corrections arrive in the background.

Example: submit question one, type question two, then click the green or red history marker to review question one.
"""

import json
import sqlite3
import time

from PySide6 import QtCore

from yt_whisper_subs import i18n
from yt_whisper_subs import library_checks
from yt_whisper_subs import library_sentence_review
from yt_whisper_subs import library_written
from yt_whisper_subs import practice_tracks


class QueuedWriting(library_written.WrittenPanel):
    """Keep writing and review independent, e.g. a delayed verb correction cannot block the next exercise."""

    def __init__(self, db, mode, *, track: practice_tracks.Track, engine) -> None:
        """Keep navigation independent of the correction worker; e.g. opening statistics cannot start sentence generation."""

        super().__init__(db, mode, engine=engine)
        self._track = track
        self._checks = library_checks.Checks.shared(db, kind=track.kind, local=track == practice_tracks.Track.LISTENING)
        self._checks.changed.connect(self._changed)
        name = i18n.tr('Questions and corrections') if track == practice_tracks.Track.VERBS else None
        self._rail = library_sentence_review.QuestionRail(name=name)
        self._rail.chosen.connect(self._jump)
        self._view.question_layout.insertWidget(0, self._rail)
        self._rail.hide()
        self._elapsed, self._restoring = 0, False
        self._opening = True
        self._draft_timer = QtCore.QTimer(self)
        self._draft_timer.setSingleShot(True)
        self._draft_timer.setInterval(300)
        self._draft_timer.timeout.connect(self._save_draft)

    def activate(self) -> None:
        """Resume the visited question or its unfinished draft; e.g. returning from Progress does not jump away from an earlier answer."""

        if not self._active or self._task or self._view.options.menu.isVisible():
            return
        self._checks.resume(self._engine)
        if self._run.row:
            if not self._run.started:
                self._run.started = time.monotonic()
            self._consult()
            self._refresh()
            return
        if not self._run.pending:
            if self._run.session:
                self._advance()
            return
        self._run.pending = False
        if self._opening:
            self._opening = False
            active = self._db.written_history(self._track.kind, self._run.session) if self._run.session else []
            if not any(row["shown_at"] is not None for row in active):
                self._run.session = self._db.written_unseen_session(self._track) or self._run.session
        if not self._run.session:
            self._prepare(cached_first=True)
            return
        try:
            cursor = json.loads(self._db.setting(self._mode.settings_key.removesuffix("_options") + "_cursor", "{}"))
        except ValueError:
            cursor = {}
        history = self._db.written_history(self._track.kind, self._run.session)
        visited = {row["id"] for row in history if row["shown_at"] is not None}
        complete = all(row["answer_text"] is not None or row["outcome"] is not None for row in history)
        if not complete and cursor.get("session") == self._run.session and cursor.get("id") in visited:
            self._jump(cursor["id"])
        else:
            self._advance()

    def _prepare(self, **kwargs) -> None:
        """Flush an unfinished answer before applying new options; e.g. failed generation cannot discard the previous draft."""

        self._save_draft()
        self._opening = False
        super()._prepare(**kwargs)

    def _advance(self, *, guard: bool = False) -> None:
        """Walk forward through answers, then find unseen feedback; e.g. completing writing opens a correction instead of a generic end card."""

        if not self._active or not self._run.session or self._task:
            return
        rows = self._db.written_history(self._track.kind, self._run.session)
        writing = self._mode.ops.active() == self._run.session
        unanswered = next((row for row in rows if row["answer_text"] is None and row["outcome"] is None), None) if writing else None
        current = self._run.row
        later = [row for row in rows if current and row["ordinal"] > current["ordinal"]
                 and (row["shown_at"] is not None or row == unanswered)]
        unseen = [row for row in rows if row["outcome"] in {"correct", "wrong"} and row["viewed_at"] is None]
        target = next(iter(later), None) or unanswered or next(iter(unseen), None)
        if target:
            self._jump(target["id"], guard=guard)
        else:
            self._overview()

    def _jump(self, presentation_id: int, *, guard: bool = False) -> None:
        """Switch saved questions while protecting the current draft; e.g. checking an old correction never erases the next answer."""

        if not self._active or self._task or self._view.options.menu.isVisible():
            return
        rows = self._db.written_history(self._track.kind, self._run.session)
        target = next((row for row in rows if row["id"] == presentation_id), None)
        if target is None:
            return
        if target["answer_text"] is None and target["outcome"] is None and target["session_status"] != "active":
            return
        next_id = next((row["id"] for row in rows if row["answer_text"] is None and row["outcome"] is None), None)
        if target["shown_at"] is None and target["id"] != next_id:
            return
        if self.before_question and not self.before_question(self._run.session, lambda: self._jump(presentation_id, guard=guard)):
            return
        self._save_draft()
        row = self._db.written_open(self._track.kind, presentation_id)
        self._db.set_setting(self._mode.settings_key.removesuffix("_options") + "_cursor", json.dumps({"session": self._run.session, "id": presentation_id}))
        self._present(row, guard=guard)
        self._refresh()

    def _present(self, row: dict, *, guard: bool = False) -> None:
        """Restore immutable answers or editable drafts; e.g. a delayed correction updates only the question currently being reviewed."""

        self._restoring = True
        try:
            super()._present(row, guard=guard)
            if row["answer_text"] is None and row["outcome"] is None:
                draft = self._db.written_draft(self._track.kind, row["id"])
                self._elapsed = draft["elapsed_ms"]
                self._view.card.editor.setPlainText(draft["text"])
                self._view.action.setText(i18n.tr('Submit & next →'))
            elif row["outcome"] == "invalid":
                self._view.card.editor.setReadOnly(True)
                self._view.card.message(i18n.tr('Exercise reported · excluded from learning statistics'))
                self._view.flag.setEnabled(False)
                self._view.action.setText(i18n.tr('Next question →'))
            elif row["grade"]:
                self._view.card.feedback(row)
                self._consult()
        finally:
            self._restoring = False
        self._input_changed()
        if self._active and (row["answer_text"] is not None or row["outcome"] is not None):
            self._view.action.setFocus()

    def _consult(self) -> None:
        """Acknowledge only feedback actually opened on the active page; e.g. a background correction on Activity remains unseen."""

        row = self._run.row
        visible = self._active and self.window().isActiveWindow() and not self.window().isMinimized()
        if visible and row and row["outcome"] in {"correct", "wrong"} and row["grade"] and row["viewed_at"] is None:
            row["viewed_at"] = self._db.written_seen(self._track.kind, row["id"])

    def _input_changed(self) -> None:
        """Autosave drafts without enabling resubmission; e.g. reviewing an answered question never changes its recorded text."""

        row = self._run.row
        if row and (row["answer_text"] is not None or row["outcome"] is not None):
            self._view.action.setEnabled(True)
            return
        super()._input_changed()
        if row and not self._restoring and self._active:
            self._draft_timer.start()

    def _save_draft(self) -> None:
        """Accumulate only time spent on this editable question; e.g. visiting old feedback cannot inflate its answer duration."""

        row = self._run.row
        if row and row["answer_text"] is None and row["outcome"] is None:
            now = time.monotonic()
            self._elapsed += max(0, round((now - self._run.started) * 1000)) if self._run.started else 0
            self._run.started = now
            self._db.written_save_draft(self._track.kind, row["id"], self._view.card.editor.toPlainText(), self._elapsed)

    def _submit(self) -> None:
        """Commit and move forward immediately; e.g. repeated Enter cannot submit the next blank sentence while correction is queued."""

        row = self._run.row
        if not self._active or self._task or not row or row["answer_text"] is not None or row["outcome"] is not None:
            return
        if self._view.options.menu.isVisible() or time.monotonic() < self._run.guard_until:
            return
        try:
            self._save_draft()
            self._run.row = self._mode.ops.submit(row["id"], self._view.card.editor.toPlainText(), self._elapsed)
        except (ValueError, OSError, sqlite3.Error) as exc:
            self._view.card.message(str(exc))
            return
        self._view.card.editor.setReadOnly(True)
        self.activity_changed.emit()
        self._checks.resume(self._engine)
        self._advance(guard=True)

    def _refresh(self) -> None:
        """Refresh compact status without disturbing input; e.g. a correction for an old question never replaces the sentence being typed."""

        if not self._run.session:
            return
        rows = self._db.written_history(self._track.kind, self._run.session)
        current = self._run.row
        self._rail.refresh(rows, current["id"] if current else None)
        self._rail.show()
        if current and current["answer_text"] is not None and current["outcome"] is None:
            status = next(row for row in rows if row["id"] == current["id"])
            labels = {"checking": i18n.tr('Correction in progress…'), "failed": i18n.tr('Correction unavailable · your answer is saved.'), "queued": i18n.tr('Correction queued…')}
            self._view.card.message(labels.get(status["state"], labels["queued"]))
            self._view.action.setText(i18n.tr('Retry correction') if status["state"] == "failed" else i18n.tr('Next question →'))
        summary = self._mode.ops.summary(self._run.session)
        pending = sum(row["answer_text"] is not None and row["outcome"] is None for row in rows)
        unseen = sum(row["outcome"] in {"correct", "wrong"} and row["viewed_at"] is None for row in rows)
        if current and current["outcome"] is not None:
            self._view.action.setText(i18n.tr('Next correction →') if all(row["answer_text"] is not None or row["outcome"] is not None for row in rows)
                                      else i18n.tr('Next question →'))
        self._view.status.setText(i18n.tr(
            '{submitted} / {planned} answered  ·  {correct} correct',
            submitted=summary['submitted'],
            planned=summary['planned'],
            correct=summary['correct'],
        )
                                  + (i18n.tr('  ·  {pending} awaiting correction', pending=pending) if pending else "")
                                  + (i18n.tr('  ·  {unseen} unseen', unseen=unseen) if unseen else ""))
        self._view.options.set_active(self._mode.ops.active() == self._run.session)
        self._view.bar.setRange(0, summary["planned"])
        self._view.bar.setValue(summary["submitted"])

    def _changed(self, presentation_id: int) -> None:
        """Apply late results only to their own answer; e.g. switching rounds cannot let an old response overwrite a new draft."""

        if self._run.closed:
            return
        if self._task:
            self.activity_changed.emit()
            return
        current = self._run.row
        if current and current["id"] == presentation_id:
            self._present(getattr(self._db, f"{self._track.kind}_get")(presentation_id))
        elif current is None and self._active and self._run.session and not self._run.pending:
            rows = self._db.written_history(self._track.kind, self._run.session)
            complete = all(row["answer_text"] is not None or row["outcome"] is not None for row in rows)
            if complete or (rows and rows[0]["session_status"] != "active"):
                self._advance(guard=True)
        self._refresh()
        self.activity_changed.emit()

    def _action(self) -> None:
        """Route submit, retry and next from durable state; e.g. failed corrections can be retried without editing or blocking other questions."""

        if not self._active or self._task or self._view.options.menu.isVisible():
            return
        if time.monotonic() < self._run.guard_until:
            return
        row = self._run.row
        if row is None:
            rows = self._db.written_history(self._track.kind, self._run.session) if self._run.session else []
            unseen = any(item["outcome"] in {"correct", "wrong"} and item["viewed_at"] is None for item in rows)
            failed = next((item for item in rows if item["outcome"] is None and item["state"] == "failed"), None)
            if unseen or (self._run.session and self._mode.ops.active() == self._run.session
                          and any(item["answer_text"] is None and item["outcome"] is None for item in rows)):
                self._advance(guard=True)
            elif failed:
                self._jump(failed["id"])
            elif not any(item["answer_text"] is not None and item["outcome"] is None for item in rows):
                self._prepare(early=self._run.empty == "not_due")
        elif row["answer_text"] is None and row["outcome"] is None:
            self._submit()
        else:
            status = next(item for item in self._db.written_history(self._track.kind, self._run.session) if item["id"] == row["id"])
            if row["outcome"] is None and status["state"] == "failed":
                self._checks.retry(row["id"], self._engine)
                self._refresh()
            else:
                self._advance(guard=True)

    def _report(self) -> None:
        """Allow disputed or pending answers to be reported; e.g. a late remote result cannot reintroduce quarantined learning evidence."""

        if self._run.row and not self._task and self._active:
            self._mode.ops.flag(self._run.row["id"])
            self._changed(self._run.row["id"])

    def _overview(self) -> None:
        """Keep session history available after writing ends; e.g. queued corrections keep arriving on the rail while answers are reviewed."""

        self._save_draft()
        rows = self._db.written_history(self._track.kind, self._run.session)
        summary = self._mode.ops.summary(self._run.session)
        pending = sum(row["answer_text"] is not None and row["outcome"] is None for row in rows)
        unseen = sum(row["outcome"] in {"correct", "wrong"} and row["viewed_at"] is None for row in rows)
        failed = any(row["outcome"] is None and row["state"] == "failed" for row in rows)
        complete = all(row["answer_text"] is not None or row["outcome"] is not None for row in rows)
        result = i18n.tr('{correct} correct · {wrong} to revisit · {submitted} answers', correct=summary['correct'], wrong=summary['wrong'], submitted=summary['submitted'])
        if unseen:
            title, action = i18n.tr('{unseen} corrections to review', unseen=unseen), i18n.tr('Review corrections')
            result += i18n.tr('\nCorrections marked • on the left have not been viewed.')
        elif pending:
            title = i18n.tr('Some corrections need a retry') if failed else i18n.tr('Waiting for corrections')
            action = i18n.tr('Review pending corrections') if failed else i18n.tr('Waiting…')
            result += i18n.tr('\n{pending} awaiting correction. New feedback will open here when it arrives.', pending=pending)
        else:
            title = i18n.tr('Round reviewed') if complete else i18n.tr('Round saved')
            action = i18n.tr('New verb round') if self._track == practice_tracks.Track.VERBS else i18n.tr('Another sentence round')
            result += i18n.tr('\nAll available corrections have been viewed. Select an answer on the left to revisit it.')
        self._idle(title, result, action)
        self._view.action.setEnabled(not pending or failed or bool(unseen))
        self._view.options.set_active(self._mode.ops.active() == self._run.session)
        self._refresh()
        self._run.at_end = not pending and not unseen
        if self._run.at_end and complete:
            self.round_reviewed.emit()

    def _finish(self) -> None:
        """End writing without cancelling submitted corrections; e.g. earlier rounds still contribute their original answers to statistics."""

        self._save_draft()
        self._mode.ops.finish(self._run.session)
        self._overview()

    def hideEvent(self, event) -> None:
        """Pause draft timing when leaving the page; e.g. time spent on Activity does not count as typing."""

        self._draft_timer.stop()
        self._save_draft()
        self._run.started = 0
        super().hideEvent(event)

    def showEvent(self, event) -> None:
        """Observe the containing window's visibility and focus; e.g. results arriving behind another app stay unread until you return."""

        self.window().installEventFilter(self)
        super().showEvent(event)

    def eventFilter(self, watched, event) -> bool:
        """Acknowledge visible feedback on focus return; e.g. restoring a minimized practice window clears only the open correction's unread dot."""

        if watched is self.window() and event.type() in {QtCore.QEvent.Type.WindowActivate, QtCore.QEvent.Type.WindowStateChange}:
            QtCore.QTimer.singleShot(0, self, self.activate)
        return super().eventFilter(watched, event)

    def shutdown(self) -> None:
        """Flush local typing while leaving the application-owned queue running; e.g. closing the dialog never loses submitted work."""

        self._save_draft()
        self._draft_timer.stop()
        super().shutdown()
