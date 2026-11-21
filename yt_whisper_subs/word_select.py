"""One probabilistic word-selection policy for vocabulary and sentence practice.

Example: a shared failure, FSRS due date and recall probability affect both modes identically.
"""

from __future__ import annotations

import numpy as np

from yt_whisper_subs import practice


def error_weight(attempts, failures):
    """Share Beta-smoothed difficulty w=1+6(f+1)/(n+3); e.g. word and verb selection react consistently to lexical failures."""

    return 1 + 6 * (np.asarray(failures) + 1) / (np.asarray(attempts) + 3)


def review_pool(idx: int, available: np.ndarray, due: np.ndarray, new: np.ndarray, unused: np.ndarray,
                *, cached_first: bool) -> np.ndarray:
    """Keep due/new balance inside eligible stock first when a round can reuse checked content."""

    candidates = available
    if cached_first and np.any(ready := available & unused):
        candidates = ready
    pool = candidates & (due if idx % 5 != 4 and np.any(candidates & due) else new)
    if not np.any(pool):
        pool = candidates & due
    if not np.any(pool):
        pool = candidates
    stock = pool & unused
    return stock if not np.any(pool & due) and np.any(stock) else pool


class Selector:
    """Mix due reviews and new words using shared failure evidence; e.g. choose distinct words without oversampling mastered terms."""

    def __init__(self, policy: practice.ReviewPolicy, rng: np.random.Generator | None = None) -> None:
        """Allow reproducible sampling checks; e.g. tests seed the generator without replacing real scheduling."""

        self._policy = policy
        self._rng = rng or np.random.default_rng()

    def weights(self, rows: list[dict], now: float) -> np.ndarray:
        """Use shared p=(f+1)/(n+3); e.g. a mistake in any mode increases the same word probability."""

        failures = np.array([row.get("failures", 0) for row in rows])
        attempts = np.array([row.get("attempts", 0) for row in rows])
        recall = np.array([self._policy.recall(row["card_json"], now) for row in rows])
        frequency = np.log1p([row.get("clicks", 0) + 2 * row.get("videos", 0) + row.get("mistakes", 0) for row in rows])
        return error_weight(attempts, failures) * (1 + 2 * (1 - recall)) * (1 + np.minimum(1, frequency / 5))

    def select(self, rows: list[dict], opts, now: float, *, cached_first: bool = False) -> list[dict]:
        """Balance due and new words, optionally using eligible checked stock before generating more."""

        rows = [row for row in rows if not row["card_json"] or row["due_at"] <= now or opts.include_early]
        if not rows:
            return []
        weights = self.weights(rows, now)
        available = np.ones(len(rows), dtype=bool)
        due = np.array([bool(row["card_json"]) and row["due_at"] <= now for row in rows])
        new = np.array([not row["card_json"] for row in rows])
        unused = np.array([row.get("unused", False) for row in rows])
        selected = []
        for idx in range(min(opts.count, len(rows))):
            pool = review_pool(idx, available, due, new, unused, cached_first=cached_first)
            probs = np.where(pool, weights, 0)
            pick = int(self._rng.choice(len(rows), p=probs / probs.sum()))
            selected.append(rows[pick])
            available[pick] = False
        return selected
