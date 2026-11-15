"""Verb-specific native presentation and optional English help on the shared typed-practice flow.

Example: a late source translation cannot overwrite the next question or reveal a transformed answer.
"""

from __future__ import annotations

from PySide6 import QtCore

from yt_whisper_subs import library_written
from yt_whisper_subs import library_verb_controls as controls
from yt_whisper_subs import library_workers
from yt_whisper_subs import verb_service


class VerbPanel(library_written.WrittenPanel):
    """Keep verb-only options and source help outside the generic submission lifecycle."""

    def __init__(self, db, *, engine: verb_service.VerbService | None = None) -> None:
        """Bind the verb domain's operations and native view; e.g. stored rounds resume with their original grammar tasks."""

        mode = library_written.Mode(key="verb", settings_key="verb_options", label="Verb transformations", tab_name="Verbs",
                                    intro="Rewrite a Dutch sentence for a new subject, tense or situation.",
                                    view=controls.VerbView(db.setting("verb_options", "")),
                                    ops=library_written.SessionOps.for_db(db, "verb"))
        super().__init__(db, mode, engine=engine or verb_service.VerbService(db))
        self._help_tasks: dict[int, library_workers.BackgroundTask] = {}
        self._view.card.meaning.requested.connect(self._english)

    def _advance(self, *, guard: bool = False) -> None:
        """Cancel help at a question boundary; e.g. an old meaning cannot appear under a new source sentence."""

        if not self._active or not self._run.session or self._task:
            return
        for task in self._help_tasks.values():
            task.cancel()
        self._help_tasks.clear()
        super()._advance(guard=guard)

    def _english(self) -> None:
        """Fetch supplemental help only for the source sentence; e.g. an older resumable round remains usable."""

        if not self._active or not self._run.row:
            return
        question_id = self._run.row["question_id"]
        if question_id in self._help_tasks:
            return

        def translate(report):
            """Tag every result with its question; e.g. a late translation cannot overwrite the next sentence."""

            try:
                return {"question_id": question_id, "english": self._engine.english(question_id, report)}
            except Exception as exc:
                return {"question_id": question_id, "error": str(exc)}

        task = library_workers.BackgroundTask(translate)
        self._help_tasks[question_id] = task
        task.signals.finished.connect(self._english_ready)
        QtCore.QThreadPool.globalInstance().start(task)

    def _english_ready(self, result: dict) -> None:
        """Apply help without grading or extra exposure; e.g. hiding the tab while translating preserves the same attempt."""

        self._help_tasks.pop(result["question_id"], None)
        row = self._run.row
        if self._run.closed or not row or row["question_id"] != result["question_id"]:
            return
        if "error" in result:
            self._view.card.meaning.failed(result["error"])
        else:
            self._view.card.meaning.translated(result["english"])

    def shutdown(self) -> None:
        """Stop supplemental help as well as the shared worker; e.g. closing remains immediate during translation."""

        for task in self._help_tasks.values():
            task.cancel()
        self._help_tasks.clear()
        super().shutdown()
