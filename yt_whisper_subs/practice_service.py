"""Prepare adaptive, cached practice rounds without coupling network work to Qt.

Example: `PracticeService(db).start(opts, report)` returns a durable resumable round ID.
"""

from __future__ import annotations

import json
import operator
import random
import re
import time
from collections.abc import Callable

from yt_whisper_subs import i18n
from yt_whisper_subs import practice_round
from yt_whisper_subs import practice
from yt_whisper_subs import practice_generate
from yt_whisper_subs import practice_ready
from yt_whisper_subs import word_select


class PracticeService:
    """Select due vocabulary and assemble independently checked three-choice questions."""

    def __init__(self, db, generator: practice_generate.Generator | None = None) -> None:
        """Allow a deterministic provider in tests; e.g. exercise the entire round without network."""

        self._db = db
        self._provider = generator or practice_generate.QuestionGenerator()

    @practice_ready.serialized("practice", practice_generate.PROMPT_VERSION)
    def start(self, opts: practice.RoundOptions, report: Callable[[str], None], *, cached_first: bool = False, background: bool = False) -> str:
        """Generate bounded batches before committing a round; e.g. cancel safely while preparing choices."""

        policy = practice.ReviewPolicy()
        candidates = self._db.word_candidates(opts.direction.value)
        if background:
            candidates = self._db.practice_spare_candidates("practice", opts, candidates)
        if not candidates:
            raise practice_round.NothingToPractice("no_words", i18n.tr('Capture Dutch words in mpv and let their translations finish first.'))
        generation = {"model": self._provider.model, "reasoning": self._provider.reasoning,
                      "prompt_version": practice_generate.PROMPT_VERSION, "direction": opts.direction.value}
        unused = self._db.practice_unused("practice", generation)
        for row in candidates:
            row["unused"] = row["lookup_id"] in unused
        selector = word_select.Selector(policy)
        selected = selector.select(candidates, opts, time.time())
        if not selected:
            raise practice_round.NothingToPractice("not_due", i18n.tr('No terms are due yet. Try the other direction, capture more words, or enable early review.'))
        build = practice_round.Build(opts=opts, report=report, cached_first=cached_first, background=background)
        plan = practice_round.Plan(candidates=candidates, initial=selected, key=operator.itemgetter("lookup_id"),
                                   select=lambda rows, n: selector.select(rows, opts.model_copy(update={"count": n}), time.time()),
                                   load=self._ensure_content)
        checked = build.fill(plan)
        questions = []
        for question in checked:
            content = json.loads(question["body_json"])
            choices = [{"text": content["answer"], "explanation": content["explanation"], "kind": "correct"},
                       *content["distractors"]]
            random.shuffle(choices)
            correct_idx = next(idx for idx, choice in enumerate(choices) if choice["kind"] == "correct")
            questions.append({"id": question["id"], "choices": choices, "correct_idx": correct_idx})
        report(i18n.tr('Ready · {len} distinct terms', len=len(questions)))
        if background:
            return self._db.practice_stage("practice", opts, questions)
        return self._db.practice_create(opts.model_dump(mode="json"), questions, policy.document())

    def _ensure_content(self, selected: list[dict], build: practice_round.Build) -> dict:
        """Maintain a small approved variant pool; e.g. cached questions remain available during API outages."""

        cached, pending = {}, []
        reserved = self._db.practice_reserved("practice", build.opts) if build.background else set()
        direction = build.opts.direction
        policy = {"model": self._provider.model, "reasoning": self._provider.reasoning,
                  "prompt_version": practice_generate.PROMPT_VERSION}
        for row in selected:
            variants = [item for item in self._db.practice_cached(row["lookup_id"], direction.value, policy) if item["id"] not in reserved]
            if variants:
                cached[row["lookup_id"]] = variants[0]
            # Build a small rotating pool, then use the least-seen variant with new button ordering.
            if not variants or (not build.cached_first and len(variants) < 3 and variants[0]["uses"] > 0):
                pending.append(row)
        for offset in range(0, len(pending), 6):
            batch = build.take(pending[offset:offset + 6])
            if not batch:
                break
            build.report(i18n.tr('Preparing and checking {len} choices…', len=len(batch)))
            try:
                results = self._provider.generate(batch, direction)
                cached.update(self._cache(batch, results, direction, policy))
            except ValueError as exc:
                for row in batch:
                    row["previous_rejection"] = str(exc)
            except Exception:
                if not all(row["lookup_id"] in cached for row in batch):
                    raise
                build.report(i18n.tr('Using previously checked choices while generation is unavailable.'))
                continue
        return cached

    def _cache(self, rows: list[dict], results: list[dict], direction, policy: dict) -> dict:
        """Validate batch identity before keeping approvals; e.g. missing or substituted results become bounded replacement work."""

        expected = {row["lookup_id"]: row for row in rows}
        checked, ready = {}, {}
        for result in results:
            draft = practice.Draft.model_validate(result["draft"])
            verdict = practice.Verdict.model_validate(result["verdict"])
            if draft.lookup_id not in expected or draft.lookup_id in checked or verdict.lookup_id != draft.lookup_id:
                raise ValueError("Invalid word-quiz generation identities")
            checked[draft.lookup_id] = draft, verdict
        if checked.keys() != expected.keys():
            raise ValueError("The provider omitted word-quiz questions")
        for key, (draft, verdict) in checked.items():
            row = expected[key]
            if not verdict.valid:
                row["previous_rejection"] = verdict.reason
                continue
            try:
                body = self._body(row, direction, draft)
            except ValueError as exc:
                row["previous_rejection"] = str(exc)
                continue
            item = {"lookup_id": key, "direction": direction.value, "body_json": json.dumps(body, ensure_ascii=False),
                    "validation_json": verdict.model_dump_json(), **policy}
            ready[key] = self._db.practice_cache(item)
        return ready

    @staticmethod
    def _body(row: dict, direction: practice.Direction, draft: practice.Draft) -> dict:
        """Anchor both directions to saved meanings; e.g. reverse context blanks every occurrence of the answer."""

        ctx = json.loads(row["context_json"])
        reverse = direction == practice.Direction.EN_NL
        prompt, answer = (row["meaning"], row["surface"]) if reverse else (row["surface"], row["meaning"])
        draft.check(answer)
        sentence = ctx["sentence"]
        if reverse:
            span = ctx["span"]
            form = sentence[span[0]:span[1]] if span else row["surface"]
            sentence = re.sub(re.escape(form), "____", sentence, flags=re.IGNORECASE)
        return {"prompt": prompt, "answer": answer, "context": sentence, "source_sentence": ctx["sentence"],
                "surface": row["surface"], "meaning": row["meaning"], "explanation": draft.explanation,
                "distractors": [item.model_dump() for item in draft.distractors]}
