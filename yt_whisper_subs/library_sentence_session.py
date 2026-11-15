"""Navigate sentence answers while independent durable corrections arrive.

Example: submit question one, type question two, then click the green or red history marker to review question one.
"""

import json
import sqlite3
import time

from PySide6 import QtCore

from yt_whisper_subs import library_sentence_checks
from yt_whisper_subs import library_sentence_review
from yt_whisper_subs import library_written


class SentenceSession(library_written.WrittenPanel):
    """Add an asynchronous session strategy to shared written controls; e.g. verb practice retains its existing deliberate correction flow."""

    def __init__(self, db, mode, *, engine) -> None:
        """Keep navigation independent of the correction worker; e.g. opening statistics cannot start sentence generation."""

        super().__init__(db, mode, engine=engine)
        self._checks = library_sentence_checks.SentenceChecks.shared(db)
        self._checks.changed.connect(self._changed)
        self._rail = library_sentence_review.QuestionRail()
        self._rail.chosen.connect(self._jump)
        self._view.question_layout.insertWidget(0, self._rail)
        self._rail.hide()
        self._elapsed, self._restoring = 0, False
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
            self._refresh()
            return
        if not self._run.pending:
            return
        self._run.pending = False
        if not self._run.session:
            self._prepare(cached_first=True)
            return
        try:
            cursor = json.loads(self._db.setting("sentence_cursor", "{}"))
        except ValueError:
            cursor = {}
        history = self._db.sentence_history(self._run.session)
        visited = {row["id"] for row in history if row["shown_at"] is not None}
        if cursor.get("session") == self._run.session and cursor.get("id") in visited:
            self._jump(cursor["id"])
        else:
            self._advance()

    def _prepare(self, **kwargs) -> None:
        """Flush an unfinished answer before applying new options; e.g. failed generation cannot discard the previous draft."""

        self._save_draft()
        super()._prepare(**kwargs)

    def _advance(self, *, guard: bool = False) -> None:
        """Choose an unanswered question without waiting for grading; e.g. queued question one does not block question two."""

        if not self._active or not self._run.session or self._task:
            return
        rows = self._db.sentence_history(self._run.session)
        target = next((row for row in rows if row["answer_text"] is None and row["outcome"] is None), None)
        if target is None or self._db.sentence_active() != self._run.session:
            self._overview()
        else:
            self._jump(target["id"], guard=guard)

    def _jump(self, presentation_id: int, *, guard: bool = False) -> None:
        """Switch saved questions while protecting the current draft; e.g. checking an old correction never erases the next answer."""

        if not self._active or self._task:
            return
        rows = self._db.sentence_history(self._run.session)
        target = next((row for row in rows if row["id"] == presentation_id), None)
        if target is None:
            return
        if target["answer_text"] is None and target["outcome"] is None and target["session_status"] != "active":
            return
        next_id = next((row["id"] for row in rows if row["answer_text"] is None and row["outcome"] is None), None)
        if target["shown_at"] is None and target["id"] != next_id:
            return
        self._save_draft()
        row = self._db.sentence_open(presentation_id)
        self._db.set_setting("sentence_cursor", json.dumps({"session": self._run.session, "id": presentation_id}))
        self._present(row, guard=guard)
        self._refresh()

    def _present(self, row: dict, *, guard: bool = False) -> None:
        """Restore immutable answers or editable drafts; e.g. a delayed correction updates only the question currently being reviewed."""

        self._restoring = True
        try:
            super()._present(row, guard=guard)
            if row["answer_text"] is None and row["outcome"] is None:
                draft = self._db.sentence_draft(row["id"])
                self._elapsed = draft["elapsed_ms"]
                self._view.card.editor.setPlainText(draft["text"])
                self._view.action.setText("Submit & next →")
            elif row["outcome"] == "invalid":
                self._view.card.editor.setReadOnly(True)
                self._view.card.message("Exercise reported · excluded from learning statistics")
                self._view.flag.setEnabled(False)
                self._view.action.setText("Next unanswered →")
            elif row["grade"]:
                self._view.card.feedback(row)
                self._view.action.setText("Next unanswered →")
        finally:
            self._restoring = False
        self._input_changed()

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
            self._db.sentence_save_draft(row["id"], self._view.card.editor.toPlainText(), self._elapsed)

    def _submit(self) -> None:
        """Commit and move forward immediately; e.g. repeated Enter cannot submit the next blank sentence while correction is queued."""

        row = self._run.row
        if not self._active or self._task or not row or row["answer_text"] is not None or row["outcome"] is not None:
            return
        if self._view.options.menu.isVisible() or time.monotonic() < self._run.guard_until:
            return
        try:
            self._save_draft()
            self._run.row = self._db.sentence_submit(row["id"], self._view.card.editor.toPlainText(), self._elapsed)
        except (ValueError, OSError, sqlite3.Error) as exc:
            self._view.card.message(str(exc))
            return
        self.activity_changed.emit()
        self._checks.resume(self._engine)
        self._advance(guard=True)

    def _refresh(self) -> None:
        """Refresh compact status without disturbing input; e.g. a correction for an old question never replaces the sentence being typed."""

        if not self._run.session:
            return
        rows = self._db.sentence_history(self._run.session)
        current = self._run.row
        self._rail.refresh(rows, current["id"] if current else None)
        self._rail.show()
        if current and current["answer_text"] is not None and current["outcome"] is None:
            status = next(row for row in rows if row["id"] == current["id"])
            labels = {"checking": "Correction in progress…", "failed": "Correction unavailable · your answer is saved.", "queued": "Correction queued…"}
            self._view.card.message(labels.get(status["state"], "Correction queued…"))
            self._view.action.setText("Retry correction" if status["state"] == "failed" else "Next unanswered →")
        summary = self._db.sentence_summary(self._run.session)
        pending = sum(row["answer_text"] is not None and row["outcome"] is None for row in rows)
        self._view.status.setText(f"{summary['submitted']} / {summary['planned']} answered  ·  {summary['correct']} correct"
                                  + (f"  ·  {max(0, pending)} awaiting correction" if pending else ""))
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
            self._present(self._db.sentence_get(presentation_id))
        self._refresh()
        self.activity_changed.emit()

    def _action(self) -> None:
        """Route submit, retry and next from durable state; e.g. failed corrections can be retried without editing or blocking other questions."""

        if not self._active or self._task or self._view.options.menu.isVisible():
            return
        row = self._run.row
        if row is None:
            self._prepare(early=self._run.empty == "not_due")
        elif row["answer_text"] is None and row["outcome"] is None:
            self._submit()
        else:
            status = next(item for item in self._db.sentence_history(self._run.session) if item["id"] == row["id"])
            if row["outcome"] is None and status["state"] == "failed":
                self._checks.retry(row["id"], self._engine)
                self._refresh()
            else:
                self._advance(guard=True)

    def _report(self) -> None:
        """Allow disputed or pending answers to be reported; e.g. a late remote result cannot reintroduce quarantined learning evidence."""

        if self._run.row and not self._task and self._active:
            self._db.sentence_flag(self._run.row["id"])
            self._changed(self._run.row["id"])

    def _overview(self) -> None:
        """Keep session history available after writing ends; e.g. queued corrections keep arriving on the rail while answers are reviewed."""

        self._save_draft()
        rows = self._db.sentence_history(self._run.session)
        complete = all(row["answer_text"] is not None or row["outcome"] is not None for row in rows)
        self._idle("Answers submitted" if complete else "Round saved",
                   "Corrections arrive in the background. Select a question on the left to review your answer.", "Another sentence round")
        self._view.options.set_active(self._db.sentence_active() == self._run.session)
        self._refresh()

    def _finish(self) -> None:
        """End writing without cancelling submitted corrections; e.g. earlier rounds still contribute their original answers to statistics."""

        self._save_draft()
        self._db.sentence_finish(self._run.session)
        self._overview()

    def hideEvent(self, event) -> None:
        """Pause draft timing when leaving the page; e.g. time spent on Activity does not count as typing."""

        self._draft_timer.stop()
        self._save_draft()
        self._run.started = 0
        super().hideEvent(event)

    def shutdown(self) -> None:
        """Flush local typing while leaving the application-owned queue running; e.g. closing the dialog never loses submitted work."""

        self._save_draft()
        self._draft_timer.stop()
        super().shutdown()
