"""Measure gradual sentence improvement from saved, minimally corrected wording.

Example: one changed word in a ten-word answer earns 90%, while whole-sentence accuracy can still be zero.
"""

from rapidfuzz import distance

from yt_whisper_subs import verb_practice


def partial(answer: str, corrected: str) -> float:
    """Score all sentence words without model calls; e.g. accepted alternative wording is compared to its own correction, not a fixed reference."""

    actual = verb_practice.normalized(answer).split()
    expected = verb_practice.normalized(corrected).split()
    # s = 1 - d / max(n, m): unit-cost word insertions, deletions and substitutions.
    return distance.Levenshtein.normalized_similarity(actual, expected)
