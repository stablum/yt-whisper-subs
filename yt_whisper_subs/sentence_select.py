"""Error-aware sentence vocabulary, morphology and life-context selection.

Example: weak superlatives and forgotten saved words receive extra probability while successful forms back off.
"""

from __future__ import annotations

import re

import numpy as np

from yt_whisper_subs import word_select
from yt_whisper_subs import sentence_practice as sentences


class Selector(word_select.Selector):
    """Combine recall, error rates and context variety; e.g. weak Dutch superlatives receive extra practice."""

    def targets(self, rows: list[dict], reviews: list[dict], opts: sentences.Options, now: float,
                *, cached_first: bool = False) -> tuple[list[dict], list[dict]]:
        """Limit exact sentence reviews while preserving shared word priorities; e.g. two review slots and eight varied contexts in a ten-question round."""

        anchors = self.select(rows, opts, now, cached_first=cached_first)
        reviews = [row for row in reviews if (opts.scene == "mixed" or row["scene"] == opts.scene)
                   and row["word_count"] <= opts.words]
        reviews.sort(key=lambda row: self._policy.recall(row["card_json"], now))
        chosen, selected, seen = [], [], set()
        limit = opts.count // 5
        for _ in range(opts.count):
            anchors = [row for row in anchors if row["word_id"] not in seen]
            reviews = [row for row in reviews if row["anchor_word_id"] not in seen]
            if reviews and (len(chosen) < limit or not anchors):
                row = reviews.pop(0)
                chosen.append(row)
                seen.add(row["anchor_word_id"])
            elif anchors:
                row = anchors.pop(0)
                selected.append(row)
                seen.add(row["word_id"])
        return chosen, selected

    def plans(self, anchors: list[dict], rows: list[dict], opts: sentences.Options, metrics: dict, now: float) -> list[dict]:
        """Offer optional companions around a fixed anchor, e.g. five choices for one extra saved word."""

        eligible = [row for row in rows if not row["card_json"] or row["due_at"] <= now or opts.include_early]
        companions = (opts.words - 1) * word_select.CANDIDATES_PER_SLOT
        counts, scenes, result = {}, {}, []
        weights = self.weights(eligible, now)
        for anchor in anchors:
            selected = [anchor]
            for _ in range(min(companions, len(eligible))):
                used = {row["word_id"] for row in selected}
                probs = np.array([0 if row["word_id"] in used else w / (1 + counts.get(row["word_id"], 0)) ** 2
                                  for row, w in zip(eligible, weights)])
                if not probs.sum():
                    break
                selected.append(eligible[int(self._rng.choice(len(eligible), p=probs / probs.sum()))])
            for row in selected:
                counts[row["word_id"]] = counts.get(row["word_id"], 0) + 1
            scene = self._scene(opts.scene, scenes, metrics.get("scenes", []))
            scenes[scene] = scenes.get(scene, 0) + 1
            words = [{**row, "request_form": self._form(row, metrics.get("forms", []))} for row in selected]
            result.append({"anchor_word_id": anchor["word_id"], "scene": scene,
                           "allow_context_change": opts.scene == "mixed", "max_words": opts.words, "words": words})
        return result

    def _scene(self, requested: str, used: dict, metrics: list[dict]) -> str:
        """Balance ordinary contexts with observed difficulty; e.g. ten questions need not all occur in a shop."""

        if requested != "mixed":
            return requested
        rates = {row["scene"]: (row["failures"] + 1) / (row["attempts"] + 3) for row in metrics}
        choices = list(sentences.SCENES)
        weights = np.array([(1 + 3 * rates.get(key, 1 / 3)) / (1 + 2 * used.get(key, 0)) for key in choices])
        return str(self._rng.choice(choices, p=weights / weights.sum()))

    def _form(self, row: dict, metrics: list[dict]) -> str:
        """Suggest valid morphological families from the saved part of speech; e.g. gradable adjectives can request a superlative."""

        pos = row["part_of_speech"] or ""
        if re.search(r"\b(verb|werkwoord|participle|deelwoord|auxiliary|modal)\b", pos, re.I):
            choices = ["person", "present", "past", "perfect", "future", "conditional", "imperative"]
        elif re.search(r"\b(adjective|bijvoeglijk)\b", pos, re.I):
            choices = ["base", "comparative", "superlative", "adjective_ending"]
        elif re.search(r"\b(noun|zelfstandig)\b", pos, re.I):
            choices = ["singular", "plural", "diminutive"]
        elif re.search(r"\b(pronoun|determiner|voornaamwoord)\b", pos, re.I):
            choices = ["person", "possessive", "plural"]
        else:
            choices = ["base"]
        rates = {item["feature"]: (item["failures"] + 1) / (item["attempts"] + 3) for item in metrics}
        weights = np.array([1 + 4 * rates.get(key, 1 / 3) for key in choices])
        return str(self._rng.choice(choices, p=weights / weights.sum()))
