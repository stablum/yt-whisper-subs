"""Orchestrate two-way sentence practice around saved target words and writing skills.

Example: correct target meanings can back off while a sentence with a syntax mistake returns for review.
"""

from __future__ import annotations

import json
import operator
import time

from yt_whisper_subs import practice_round
from yt_whisper_subs import practice
from yt_whisper_subs import practice_ready
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

    @practice_ready.serialized("sentence", sentence_generate.PROMPT_VERSION)
    def start(self, opts: sentences.Options, report, *, cached_first: bool = True, background: bool = False) -> str:
        """Prepare approved new sentences and due writing reviews before replacing a round; e.g. a provider outage preserves old progress."""

        policy, now = practice.ReviewPolicy(), time.time()
        rows = self._db.word_candidates(opts.direction.value)
        if background:
            rows = self._db.practice_spare_candidates("sentence", opts, rows)
        if not rows:
            raise practice_round.NothingToPractice("no_words", "Save and translate some Dutch vocabulary words before sentence practice.")
        generation = {"direction": opts.direction.value, "model": self._provider.model,
                      "reasoning": self._provider.reasoning, "prompt_version": sentence_generate.PROMPT_VERSION}
        selector = sentence_select.Selector(policy)
        metrics = sentence_stats.metrics(self._db, opts.direction.value)
        pool = self._db.sentence_pool(opts, generation)
        unused = {row["anchor_word_id"] for row in pool}
        for row in rows:
            row["unused"] = row["word_id"] in unused
        reviews = self._db.sentence_due(generation, now)
        if background:
            targets = {row["word_id"] for row in rows}
            reserved = self._db.practice_reserved("sentence", opts)
            reviews = [row for row in reviews if row["anchor_word_id"] in targets and row["id"] not in reserved]
        chosen, selected = selector.targets(rows, reviews, opts, now)
        if not chosen and not selected:
            raise practice_round.NothingToPractice("not_due", "Your sentence targets and writing reviews are not due yet. Try early practice if you wish.")
        build = practice_round.Build(opts=opts, report=report, cached_first=cached_first, background=background)

        def load(anchors, job):
            """Plan only the missing sentence targets; e.g. checked cached sentences never get regenerated during a top-up."""

            plans = selector.plans(anchors, rows, opts, metrics, now)
            by_id = {row["word_id"]: row for row in anchors}
            examples = self._db.sentence_examples(list(by_id), opts.direction.value)
            for item in plans:
                item["previous_rejection"] = by_id[item["anchor_word_id"]].get("previous_rejection", "")
                item["recent_sentences"] = examples.get(item["anchor_word_id"], [])
            checked = self._content(plans, generation, metrics, job)
            for item in plans:
                by_id[item["anchor_word_id"]]["previous_rejection"] = item["previous_rejection"]
            return {row["anchor_word_id"]: row for row in checked}

        plan = practice_round.Plan(candidates=rows, initial=selected, key=operator.itemgetter("word_id"), load=load,
                                   seed={row["anchor_word_id"]: row for row in chosen},
                                   select=lambda rows, n: selector.select(rows, opts.model_copy(update={"count": n}), now))
        questions = build.fill(plan)
        report(f"{len(questions)} checked sentence translations ready")
        if background:
            return self._db.practice_stage("sentence", opts, questions)
        return self._db.sentence_create(opts, questions, policy.document())

    def _content(self, plans: list[dict], policy: dict, metrics: dict, build: practice_round.Build) -> list[dict]:
        """Cache exact target-context sets before shared top-ups; e.g. only rejected pairs need replacement drafts."""

        ready, pending = {}, []
        reserved = self._db.practice_reserved("sentence", build.opts) if build.background else set()
        pool = self._db.sentence_pool(build.opts, policy)
        for plan in plans:
            cached = [row for row in pool if row["anchor_word_id"] == plan["anchor_word_id"] and row["id"] not in reserved]
            cached = cached or [row for row in self._db.sentence_cached(plan, policy) if row["uses"] == 0 and row["id"] not in reserved]
            if cached:
                ready[plan["anchor_word_id"]] = cached[0]
            if not cached:
                pending.append(plan)
        for offset in range(0, len(pending), 4):
            batch = build.take(pending[offset:offset + 4])
            if not batch:
                break
            document = [self._document(plan) for plan in batch]
            originals = {plan["anchor_word_id"]: plan for plan in batch}
            build.report(f"Preparing and checking {len(document)} sentence pairs…")
            try:
                generated = self._provider.generate(document, build.opts.direction, metrics["kinds"])
                approved, rejected = self._cache(generated, originals, policy)
            except ValueError as exc:
                approved, rejected = {}, dict.fromkeys(originals, str(exc))
            ready.update(approved)
            for row in batch:
                if row["anchor_word_id"] in rejected:
                    row["previous_rejection"] = rejected[row["anchor_word_id"]]
        return [ready[plan["anchor_word_id"]] for plan in plans if plan["anchor_word_id"] in ready]

    @staticmethod
    def _document(plan: dict) -> dict:
        """Send linguistic evidence only; e.g. generated examples never need video URLs or private paths."""

        words = [{"word_id": row["word_id"], "surface": row["surface"], "lemma": row["lemma"],
                  "meaning": row["meaning"], "lemma_meaning": row["lemma_meaning"],
                  "part_of_speech": row["part_of_speech"], "request_form": row["request_form"],
                  "context": json.loads(row["context_json"]), "note": row["note"] or ""} for row in plan["words"]]
        return {"anchor_word_id": plan["anchor_word_id"], "scene": plan["scene"],
                "allow_context_change": plan["allow_context_change"], "words": words,
                "previous_rejection": plan.get("previous_rejection", ""),
                "recent_sentences": plan.get("recent_sentences", [])}

    def _cache(self, generated: list[dict], plans: dict, policy: dict) -> tuple[dict, dict]:
        """Check a whole model batch before persisting approvals; e.g. duplicate or substituted IDs cannot partially seed playable content."""

        checked, approved, rejected = {}, {}, {}
        for item in generated:
            draft = sentences.Draft.model_validate(item["draft"])
            verdict = sentences.Verdict.model_validate(item["verdict"])
            if draft.anchor_word_id not in plans or draft.anchor_word_id in checked or verdict.anchor_word_id != draft.anchor_word_id:
                raise ValueError("Invalid sentence generation IDs")
            plan = plans[draft.anchor_word_id]
            if verdict.valid:
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
                                     explanation=content["explanation"], words=words, errors=[], vocabulary=[])
        else:
            grade = self._provider.grade({"direction": row["direction"], "exercise": content, "learner_answer": row["answer_text"]})
        grade.check(row["answer_text"], content, row["direction"])
        report("Saving sentence and vocabulary feedback…")
        return self._db.sentence_grade(presentation_id, grade, {"model": "approved_exact_match" if exact else self._provider.model,
                                                              "reasoning": "none" if exact else self._provider.reasoning,
                                                              "grade_version": sentence_generate.GRADE_VERSION})
