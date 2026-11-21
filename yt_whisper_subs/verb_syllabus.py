"""Constrained Dutch targets and error-aware spaced selection.

Example: rare formal subjunctives remain available without inventing a full modern Italian-style paradigm.
"""

from __future__ import annotations

import numpy as np

from yt_whisper_subs import practice
from yt_whisper_subs import verb_practice as verbs
from yt_whisper_subs import word_select


# Lemma, conjugation family and passive eligibility; lexical properties constrain generation.
CATALOG = (
    ("zijn", "irregular", False), ("hebben", "irregular", False), ("worden", "strong", False),
    ("gaan", "irregular", False), ("komen", "strong", False), ("lopen", "strong", False),
    ("werken", "regular", False), ("wonen", "regular", False), ("maken", "regular", True),
    ("kopen", "irregular", True), ("zien", "strong", True), ("geven", "strong", True),
    ("nemen", "strong", True), ("eten", "strong", True), ("drinken", "strong", True),
    ("schrijven", "strong", True), ("lezen", "strong", True), ("spreken", "strong", False),
    ("vinden", "strong", True), ("denken", "irregular", False), ("brengen", "irregular", True),
    ("weten", "irregular", False), ("kunnen", "modal", False), ("moeten", "modal", False),
    ("mogen", "modal", False), ("willen", "modal", False), ("opstaan", "separable", False),
    ("aankomen", "separable", False), ("opbellen", "separable", True), ("uitleggen", "separable", True),
    ("zich herinneren", "reflexive", False), ("zich vergissen", "reflexive", False),
    ("leven", "regular", False), ("rusten", "regular", False), ("helpen", "strong", True),
    ("kosten", "regular", False),
)
_SUBJUNCTIVES = {"zijn": ("het",), "gaan": ("het",), "leven": ("de_koning", "de_jarigen"),
                "rusten": ("hij",), "nemen": ("men",),
                "mogen": ("hij", "zij_pl"), "kosten": ("het",)}


def candidates(catalog: list[dict], states: list[dict], opts: verbs.Options) -> list[dict]:
    """Enumerate valid targets and attach durable state; e.g. only zijn supplies a past subjunctive."""

    saved = {(row["verb_id"], row["tense"], row["subject"], row["structure"]): row for row in states}
    result = []
    for verb in catalog:
        if opts.source == "vocabulary" and not verb["lookups"]:
            continue
        if opts.source == "starter" and not verb["starter"]:
            continue
        if not verb["starter"] and not verb["lookups"]:
            continue
        for tense in verbs.Tense:
            if not _in_focus(tense, opts.focus):
                continue
            for subject in verbs.SUBJECTS:
                if opts.subject != "all" and subject != opts.subject:
                    continue
                structures = _structures(verb, tense, subject)
                for structure in structures:
                    if opts.structure != "all" and structure != opts.structure:
                        continue
                    key = (verb["id"], tense.value, subject, structure)
                    result.append({**verb, "verb_id": verb["id"], "tense": tense.value, "subject": subject,
                                   "structure": structure, "card_json": None, "due_at": None,
                                   **saved.get(key, {})})
    return result


def _in_focus(tense: verbs.Tense, focus: str) -> bool:
    """Map learner-facing groups to grammar forms; e.g. conditionals includes the perfect conditional."""

    groups = {"all": set(verbs.Tense), "everyday": {verbs.Tense.PRESENT, verbs.Tense.PAST, verbs.Tense.PERFECT},
              "conditionals": {verbs.Tense.CONDITIONAL, verbs.Tense.CONDITIONAL_PERFECT},
              "subjunctives": {verbs.Tense.SUBJUNCTIVE, verbs.Tense.SUBJUNCTIVE_PAST},
              "imperatives": {verbs.Tense.IMPERATIVE}}
    return tense in groups[focus] if focus in groups else tense.value == focus


def _structures(verb: dict, tense: verbs.Tense, subject: str) -> list[str]:
    """Constrain mood, person and voice together; e.g. commands never acquire an impossible future-perfect target."""

    if tense == verbs.Tense.SUBJUNCTIVE:
        return ["wish"] if subject in _SUBJUNCTIVES.get(verb["lemma"], ()) else []
    if tense == verbs.Tense.SUBJUNCTIVE_PAST:
        return ["formal"] if verb["lemma"] == "zijn" and subject == "het" else []
    if tense == verbs.Tense.IMPERATIVE:
        return ["command"] if subject in {"jij", "jullie"} and verb["family"] != "modal" else []
    if subject in {"de_koning", "de_jarigen"}:
        return []
    result = ["statement", "question", "subordinate", "negative"]
    if verb["transitive"] and subject not in {"het", "men"}:
        result.append("passive")
    if tense in {verbs.Tense.CONDITIONAL, verbs.Tense.CONDITIONAL_PERFECT}:
        result.append("if_clause")
    return result


class Selector:
    """Combine FSRS retention with marginal error rates; e.g. agreement trouble generalizes across different verbs."""

    def __init__(self, policy: practice.ReviewPolicy, rng: np.random.Generator | None = None) -> None:
        """Allow reproducible selection tests; e.g. a seeded generator verifies weak-form preference."""

        self._policy = policy
        self._rng = rng or np.random.default_rng()

    def weights(self, rows: list[dict], metrics: list[dict], now: float) -> np.ndarray:
        """Use Beta-smoothed error probability p=(f+1)/(n+3); e.g. one miss does not imply certain failure."""

        rates = {(row["axis"], str(row["value"])): (row["failures"] + 1) / (row["attempts"] + 3)
                 for row in metrics}
        # w combines marginal difficulty with FSRS forgetting probability 1-R, never response speed.
        difficulty = np.array([np.mean([rates.get((axis, str(row[f"{axis}_id"] if axis == "verb" else row[axis])), 1 / 3)
                                        for axis in verbs.AXES]) for row in rows])
        recall = np.array([self._policy.recall(row["card_json"], now) for row in rows])
        rare = np.array([0.18 if row["tense"] in {"subjunctive", "subjunctive_past", "future_perfect"} else 1.0 for row in rows])
        # Captured verbs receive w_vocab=3 plus bounded repetition evidence; starter-only verbs keep weight one.
        vocab = np.array([3 + min(2, np.log1p(row["clicks"] + 2 * row["videos"])) if row["lookups"] else 1 for row in rows])
        _, inverse, counts = np.unique([row["tense"] for row in rows], return_inverse=True, return_counts=True)
        # Divide by n_tau, the number of targets in tense tau, so a large paradigm cannot drown out a small mood.
        lexical = word_select.error_weight([row.get("word_attempts", 0) for row in rows], [row.get("word_failures", 0) for row in rows])
        return (1 + 5 * difficulty) * (1 + 2 * (1 - recall)) * rare * vocab * lexical / counts[inverse]

    def select(self, rows: list[dict], metrics: list[dict], opts: verbs.Options, now: float, *, cached_first: bool = False) -> list[dict]:
        """Prefer due weak targets with exploration and verb variety; e.g. mastered combinations wait until due."""

        rows = [row for row in rows if not row["card_json"] or row["due_at"] <= now or opts.include_early]
        if not rows:
            return []
        weights = self.weights(rows, metrics, now)
        available = np.ones(len(rows), dtype=bool)
        due = np.array([bool(row["card_json"]) and row["due_at"] <= now for row in rows])
        new = np.array([not row["card_json"] for row in rows])
        unused = np.array([row.get("unused", False) for row in rows])
        used = set()
        chosen = []
        for idx in range(min(opts.count, len(rows))):
            pool = word_select.review_pool(idx, available, due, new, unused, cached_first=cached_first)
            varied = pool & np.array([row["verb_id"] not in used for row in rows])
            if np.any(varied):
                pool = varied
            # p_i = w_i / sum(w) over the eligible pool; sampling is without replacement.
            probs = np.where(pool, weights, 0)
            pick = int(self._rng.choice(len(rows), p=probs / probs.sum()))
            chosen.append(rows[pick])
            used.add(rows[pick]["verb_id"])
            available[pick] = False
        return chosen
