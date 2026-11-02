"""Prepare adaptive, cached practice rounds without coupling network work to Qt.

Example: `PracticeService(db).start(opts, report)` returns a durable resumable round ID.
"""

from __future__ import annotations

import json
import random
import re
import time
from collections.abc import Callable

from yt_whisper_subs import practice
from yt_whisper_subs import practice_generate


class PracticeService:
    """Select due vocabulary and assemble independently checked three-choice questions."""

    def __init__(self, db, generator: practice_generate.Generator | None = None) -> None:
        """Allow a deterministic provider in tests; e.g. exercise the entire round without network."""

        self._db = db
        self._generator = generator or practice_generate.QuestionGenerator()

    def start(self, opts: practice.RoundOptions, report: Callable[[str], None]) -> str:
        """Generate bounded batches before committing a round; e.g. cancel safely while preparing choices."""

        policy = practice.ReviewPolicy()
        candidates = self._db.practice_candidates(opts.direction.value)
        if not candidates:
            raise ValueError("No translated Dutch words are ready. Capture words in mpv and let their translations finish first.")
        selected = policy.select(candidates, opts, time.time())
        if not selected:
            raise ValueError("No terms are due yet. Try the other direction, capture more words, or enable early review.")
        cached = self._ensure_content(selected, opts.direction, report)
        questions = []
        for row in selected:
            if not (question := cached.get(row["lookup_id"])):
                continue
            content = json.loads(question["body_json"])
            choices = [{"text": content["answer"], "explanation": content["explanation"], "kind": "correct"},
                       *content["distractors"]]
            random.shuffle(choices)
            correct_idx = next(idx for idx, choice in enumerate(choices) if choice["kind"] == "correct")
            questions.append({"id": question["id"], "choices": choices, "correct_idx": correct_idx})
        report(f"Ready · {len(questions)} distinct terms (requested {opts.count})")
        return self._db.practice_create(opts.model_dump(mode="json"), questions, policy.document())

    def _ensure_content(self, selected: list[dict], direction: practice.Direction, report: Callable[[str], None]) -> dict:
        """Maintain a small approved variant pool; e.g. cached questions remain available during API outages."""

        cached, pending = {}, []
        for row in selected:
            variants = self._db.practice_cached(row["lookup_id"], direction.value, practice_generate.PROMPT_VERSION)
            if variants:
                cached[row["lookup_id"]] = variants[0]
            # Build a small rotating pool, then use the least-seen variant with new button ordering.
            if not variants or (len(variants) < 3 and variants[0]["uses"] > 0):
                pending.append(row)
        for offset in range(0, len(pending), 6):
            batch = pending[offset:offset + 6]
            report(f"Preparing and checking choices · {offset + 1}–{offset + len(batch)} of {len(pending)}")
            try:
                results = self._generator.generate(batch, direction)
            except Exception:
                if not all(row["lookup_id"] in cached for row in batch):
                    raise
                report("Using previously checked choices while generation is unavailable.")
                continue
            rows = {row["lookup_id"]: row for row in batch}
            for result in results:
                draft = practice.Draft.model_validate(result["draft"])
                if not result["verdict"]["valid"]:
                    report(f"Excluded an unclear question: {result['verdict']['reason']}")
                    continue
                row = rows[draft.lookup_id]
                body = self._body(row, direction, draft)
                item = {"lookup_id": row["lookup_id"], "direction": direction.value,
                        "body_json": json.dumps(body, ensure_ascii=False),
                        "validation_json": json.dumps(result["verdict"], ensure_ascii=False),
                        "model": self._generator.model, "reasoning": self._generator.reasoning,
                        "prompt_version": practice_generate.PROMPT_VERSION}
                cached[row["lookup_id"]] = self._db.practice_cache(item)
        return cached

    @staticmethod
    def _body(row: dict, direction: practice.Direction, draft: practice.Draft) -> dict:
        """Anchor both directions to saved meanings; e.g. reverse context blanks every occurrence of the answer."""

        ctx = json.loads(row["context_json"])
        reverse = direction == practice.Direction.EN_NL
        prompt, answer = (row["meaning"], row["surface"]) if reverse else (row["surface"], row["meaning"])
        draft.check(answer)
        sentence = ctx["sentence"]
        if reverse:
            sentence = re.sub(re.escape(row["surface"]), "____", sentence, flags=re.IGNORECASE)
        return {"prompt": prompt, "answer": answer, "context": sentence, "source_sentence": ctx["sentence"],
                "surface": row["surface"], "meaning": row["meaning"], "explanation": draft.explanation,
                "distractors": [item.model_dump() for item in draft.distractors]}
