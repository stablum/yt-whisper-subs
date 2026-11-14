"""Orchestrate two-way sentence practice around saved target words and writing skills.

Example: correct target meanings can back off while a sentence with a syntax mistake returns for review.
"""

from __future__ import annotations

import json
import time

from yt_whisper_subs import practice
from yt_whisper_subs import practice_service
from yt_whisper_subs import sentence_generate
from yt_whisper_subs import sentence_practice as sentences
from yt_whisper_subs import sentence_select
from yt_whisper_subs import sentence_stats
from yt_whisper_subs import verb_practice


class SentenceService:
    """Orchestrate language work outside transactions and UI events; e.g. slow correction leaves the first attempt durable."""

    def __init__(self, db, provider: sentence_generate.Provider | None = None) -> None:
        """Allow an offline provider for deterministic tests; e.g. no user catalog is needed for verification."""

        self._db = db
        self._provider = provider or sentence_generate.SentenceGenerator()

    def start(self, opts: sentences.Options, report, *, cached_first: bool = True) -> str:
        """Prepare approved new sentences and due writing reviews before replacing a round; e.g. a provider outage preserves old progress."""

        policy, now = practice.ReviewPolicy(), time.time()
        rows = self._db.sentence_candidates(opts.direction.value)
        if not rows:
            raise practice_service.NothingToPractice("no_words", "Save and translate some Dutch vocabulary words before sentence practice.")
        generation = {"direction": opts.direction.value, "model": self._provider.model,
                      "reasoning": self._provider.reasoning, "prompt_version": sentence_generate.PROMPT_VERSION}
        selector = sentence_select.Selector(policy)
        metrics = sentence_stats.metrics(self._db, opts.direction.value)
        anchors = selector.select(rows, opts, now)
        reviews = self._db.sentence_due(generation, now)
        if opts.scene != "mixed":
            reviews = [row for row in reviews if row["scene"] == opts.scene]
        reviews.sort(key=lambda row: policy.recall(row["card_json"], now))
        chosen, selected, seen = [], [], set()
        for idx in range(opts.count):
            anchors = [row for row in anchors if row["word_id"] not in seen]
            reviews = [row for row in reviews if row["anchor_word_id"] not in seen]
            if reviews and (not anchors or idx % 5 != 4):
                row = reviews.pop(0)
                chosen.append(row)
                seen.add(row["anchor_word_id"])
            elif anchors:
                row = anchors.pop(0)
                selected.append(row)
                seen.add(row["word_id"])
        if not chosen and not selected:
            raise practice_service.NothingToPractice("not_due", "Your sentence targets and writing reviews are not due yet. Try early practice if you wish.")
        plans = selector.plans(selected, rows, opts, metrics, now)
        chosen.extend(self._content(plans, opts, generation, metrics, report, cached_first))
        report(f"{len(chosen)} checked sentence translations ready")
        return self._db.sentence_create(opts, chosen, policy.document())

    def _content(self, plans: list[dict], opts, policy: dict, metrics: dict, report, cached_first: bool) -> list[dict]:
        """Cache exact target-context sets and repair rejected pairs once; e.g. supporting words are never constrained to the saved list."""

        ready, pending = {}, []
        for plan in plans:
            cached = self._db.sentence_cached(plan, policy)
            if cached:
                ready[plan["anchor_word_id"]] = cached[0]
            if not cached or (not cached_first and len(cached) < 3 and cached[0]["uses"]):
                pending.append(plan)
        for offset in range(0, len(pending), 4):
            document = [self._document(plan) for plan in pending[offset:offset + 4]]
            originals = {plan["anchor_word_id"]: plan for plan in pending[offset:offset + 4]}
            for attempt in range(2):
                report(f"{'Preparing' if not attempt else 'Refining'} and checking {len(document)} sentence pairs…")
                try:
                    generated = self._provider.generate(document, opts.direction, metrics["kinds"])
                except Exception:
                    if all(row["anchor_word_id"] in ready for row in document):
                        break
                    raise
                approved, rejected = self._cache(generated, {row["anchor_word_id"]: originals[row["anchor_word_id"]] for row in document}, policy)
                ready.update(approved)
                if not rejected:
                    break
                document = [{**row, "previous_rejection": rejected[row["anchor_word_id"]]}
                            for row in document if row["anchor_word_id"] in rejected]
        if plans and not ready:
            raise practice_service.NothingToPractice("no_questions", "No sentence pair passed the language check. Try another round or context.")
        return [ready[plan["anchor_word_id"]] for plan in plans if plan["anchor_word_id"] in ready]

    @staticmethod
    def _document(plan: dict) -> dict:
        """Send linguistic evidence only; e.g. generated examples never need video URLs or private paths."""

        words = [{"word_id": row["word_id"], "surface": row["surface"], "lemma": row["lemma"],
                  "meaning": row["meaning"], "lemma_meaning": row["lemma_meaning"],
                  "part_of_speech": row["part_of_speech"], "request_form": row["request_form"],
                  "context": json.loads(row["context_json"]), "note": row["note"] or ""} for row in plan["words"]]
        return {"anchor_word_id": plan["anchor_word_id"], "scene": plan["scene"],
                "allow_context_change": plan["allow_context_change"], "words": words}

    def _cache(self, generated: list[dict], plans: dict, policy: dict) -> tuple[dict, dict]:
        """Check a whole model batch before persisting approvals; e.g. duplicate or substituted IDs cannot partially seed playable content."""

        checked, approved, rejected = {}, {}, {}
        for item in generated:
            draft = sentences.Draft.model_validate(item["draft"])
            verdict = sentences.Verdict.model_validate(item["verdict"])
            if draft.anchor_word_id not in plans or draft.anchor_word_id in checked or verdict.anchor_word_id != draft.anchor_word_id:
                raise ValueError("Invalid sentence generation IDs")
            plan = plans[draft.anchor_word_id]
            draft.check({word["word_id"] for word in plan["words"]}, "mixed" if plan["allow_context_change"] else plan["scene"])
            checked[draft.anchor_word_id] = draft, verdict
        if set(checked) != set(plans):
            raise ValueError("The provider omitted sentence exercises")
        for key, (draft, verdict) in checked.items():
            if verdict.valid:
                approved[key] = self._db.sentence_cache(draft, verdict, plans[key], policy)
            else:
                rejected[key] = verdict.reason
        return approved, rejected

    def check(self, presentation_id: int, report) -> dict:
        """Check a durable attempt with alternative-aware correction; e.g. an exact approved answer uses no remote grader."""

        row = self._db.sentence_get(presentation_id)
        if row["answer_text"] is None:
            raise ValueError("Submit your translated sentence first")
        if row["outcome"] is not None:
            return row
        content = row["content"]
        answer_key = "dutch" if row["direction"] == "en_nl" else "english"
        source_key = "english" if row["direction"] == "en_nl" else "dutch"
        # Approved alternatives may replace a saved lexeme with a synonym, which still needs per-word assessment.
        exact = verb_practice.normalized(row["answer_text"]) == verb_practice.normalized(content[answer_key])
        report("Checking your translation…")
        if exact:
            words = []
            for item in content["words"]:
                forms = [sentences.FormResult(feature=key,
                                              correct=None if row["direction"] == "nl_en" and key == "adjective_ending" else True,
                                              explanation="Approved translation; Dutch-only endings are not tested in English.")
                         for key in item["features"]]
                form_correct = True if any(value.correct for value in forms) else None
                words.append(sentences.WordResult(word_id=item["word_id"], meaning_correct=True, target_used=True, form_correct=form_correct,
                                                   explanation=item["explanation"], forms=forms))
            grade = sentences.Grade(confident=True, correct=True, corrected=row["answer_text"], corrected_meaning=content[source_key],
                                     explanation=content["explanation"], words=words, errors=[])
        else:
            grade = self._provider.grade({"direction": row["direction"], "exercise": content, "learner_answer": row["answer_text"]})
        grade.check(row["answer_text"], content)
        report("Saving sentence and vocabulary feedback…")
        return self._db.sentence_grade(presentation_id, grade, {"model": "approved_exact_match" if exact else self._provider.model,
                                                              "reasoning": "none" if exact else self._provider.reasoning,
                                                              "grade_version": sentence_generate.GRADE_VERSION})
