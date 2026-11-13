"""Coordinate adaptive target selection, checked content and durable answer grading.

Example: `VerbService(db).start(Options(), report)` prepares a round without exposing its answers in the UI.
"""

from __future__ import annotations

import json
import time

from yt_whisper_subs import practice
from yt_whisper_subs import practice_service
from yt_whisper_subs import verb_generate
from yt_whisper_subs import verb_practice as verbs
from yt_whisper_subs import verb_stats
from yt_whisper_subs import verb_syllabus


class VerbService:
    """Keep language-provider calls outside SQL transactions and native widgets."""

    def __init__(self, db, provider: verb_generate.Provider | None = None) -> None:
        """Inject an isolated provider when needed; e.g. tests never spend API credits."""

        self._db = db
        self._provider = provider or verb_generate.VerbGenerator()

    def start(self, opts: verbs.Options, report, *, cached_first: bool = True) -> str:
        """Build the whole approved round before replacing another; e.g. failures preserve existing practice."""

        policy = practice.ReviewPolicy()
        candidates = verb_syllabus.candidates(self._db.verb_catalog(), self._db.verb_targets(), opts)
        if not candidates:
            raise practice_service.NothingToPractice("no_questions", "These filters have no valid Dutch forms. Change the round options.")
        selected = verb_syllabus.Selector(policy).select(candidates, verb_stats.metrics(self._db), opts, time.time())
        if not selected:
            raise practice_service.NothingToPractice("not_due", "These verb forms are not due yet. Try other forms or practise early.")
        selected = [self._db.verb_target(row) for row in selected]
        questions = self._content(selected, report, cached_first)
        report(f"{len(questions)} checked verb exercises ready")
        return self._db.verb_create(opts, questions, policy.document())

    def _content(self, targets: list[dict], report, cached_first: bool) -> list[dict]:
        """Cache independent approvals and rotate variants; e.g. rejected content is omitted rather than silently accepted."""

        policy = {"model": self._provider.model, "reasoning": self._provider.reasoning,
                  "prompt_version": verb_generate.PROMPT_VERSION}
        ready, pending = {}, []
        for row in targets:
            cached = self._db.verb_cached(row["target_id"], policy)
            if cached:
                ready[row["target_id"]] = cached[0]
            if not cached or (not cached_first and len(cached) < 3 and cached[0]["uses"]):
                pending.append(row)
        for offset in range(0, len(pending), 4):
            batch = pending[offset:offset + 4]
            document = [{key: row[key] for key in ("target_id", "lemma", "family", "transitive", "tense", "subject", "structure")}
                        for row in batch]
            for attempt in range(2):
                report(f"{'Preparing' if attempt == 0 else 'Refining'} and checking {len(document)} verb exercises…")
                try:
                    approved, rejected = self._checked(document, policy)
                except Exception:
                    if all(row["target_id"] in ready for row in document):
                        break
                    raise
                ready.update(approved)
                document = [{**row, "previous_rejection": rejected[row["target_id"]]}
                            for row in document if row["target_id"] in rejected]
                if not document:
                    break
        if not ready:
            raise practice_service.NothingToPractice("no_questions", "No transformations passed the language check. Try another round.")
        return [ready[row["target_id"]] for row in targets if row["target_id"] in ready]

    def _checked(self, targets: list[dict], policy: dict) -> tuple[dict, dict]:
        """Validate exact IDs and preserve rejections for one bounded repair; e.g. an audit cannot silently omit a target."""

        generated = self._provider.generate(targets)
        expected = {row["target_id"] for row in targets}
        approved, rejected, checked = {}, {}, set()
        validated = []
        for item in generated:
            draft = verbs.Draft.model_validate(item["draft"])
            verdict = verbs.Verdict.model_validate(item["verdict"])
            if draft.target_id not in expected or draft.target_id in checked or verdict.target_id != draft.target_id:
                raise ValueError("Invalid verb generation IDs")
            checked.add(draft.target_id)
            validated.append((draft, verdict))
        if checked != expected:
            raise ValueError("The provider omitted verb exercises")
        for draft, verdict in validated:
            if verdict.valid:
                approved[draft.target_id] = self._db.verb_cache(draft, verdict, policy)
            else:
                rejected[draft.target_id] = verdict.reason
        return approved, rejected

    def check(self, presentation_id: int, report) -> dict:
        """Grade only a durable first attempt; e.g. an exact approved alternative needs no network request."""

        row = self._db.verb_get(presentation_id)
        if row["answer_text"] is None:
            raise ValueError("Submit your sentence before checking it")
        if row["outcome"] is not None:
            return row
        content = row["content"]
        accepted = [content["answer"], *content["alternatives"]]
        exact = verbs.normalized(row["answer_text"]) in {verbs.normalized(text) for text in accepted}
        report("Checking your sentence…")
        if exact:
            grade = verbs.Grade(confident=True, correct=True, corrected=row["answer_text"],
                                explanation=content["explanation"], errors=[],
                                axes=[verbs.Axis(axis=axis, correct=True) for axis in verbs.AXES])
        else:
            target = {key: row[key] for key in ("lemma", "family", "tense", "subject", "structure")}
            grade = self._provider.grade({"target": target, "exercise": content, "learner_answer": row["answer_text"]})
        grade.check(row["answer_text"])
        report("Saving correction…")
        provenance = {"model": "approved_exact_match" if exact else self._provider.model,
                      "reasoning": "none" if exact else self._provider.reasoning, "grade_version": verb_generate.GRADE_VERSION}
        return self._db.verb_grade(presentation_id, grade, provenance)
