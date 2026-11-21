"""Define bilingual sentence-building questions and instant, alternative-aware grading."""

import re

import pydantic as pd
from rapidfuzz.distance import LCSseq, Levenshtein

from yt_whisper_subs import i18n
from yt_whisper_subs import practice


PATTERNS = {
    "subordinate": i18n.mark("Subordinate clauses"),
    "inversion": i18n.mark("Inversion after a fronted phrase or clause"),
    "relative": i18n.mark("Relative clauses"),
    "verb_cluster": i18n.mark("Verb clusters"),
    "separable": i18n.mark("Separable verbs"),
}


class Options(pd.BaseModel):
    """Configure a round, e.g. ten sentences with two extra blocks each."""

    count: int = pd.Field(default=10, ge=1, le=30)
    extras: int = pd.Field(default=2, ge=0, le=4)
    include_early: bool = False


class Extra(practice.Content):
    """Keep a plausible extra block and an explanation of why it does not fit."""

    text: str = pd.Field(min_length=1, max_length=60)
    why: str = pd.Field(min_length=3, max_length=450)


class Draft(practice.Content):
    """Store a complex sentence as movable phrases, e.g. a verb separate from its subject."""

    word_id: int
    english: str = pd.Field(min_length=12, max_length=330)
    dutch: str = pd.Field(min_length=12, max_length=330)
    tiles: list[str] = pd.Field(min_length=5, max_length=10)
    extras: list[Extra] = pd.Field(max_length=4)
    pattern: str
    explanation: str = pd.Field(min_length=16, max_length=850)
    target_form: str = pd.Field(min_length=1, max_length=80)

    @property
    def bank(self) -> list[str]:
        """Address tiles by stable ID, including extra blocks after the solution tiles."""

        return self.tiles + [extra.text for extra in self.extras]

    def check(self, word_id: int, extras: int) -> None:
        """Reject malformed builds before audit, e.g. two visually indistinguishable tiles."""

        if self.word_id != word_id or self.pattern not in PATTERNS or len(self.extras) != extras:
            raise ValueError("Sentence builder targets, patterns or extra block counts do not match")
        labels = [normalized(tile) for tile in self.bank]
        if (not all(labels) or len(set(labels)) != len(labels)
                or any(len(tile) > 60 or tile != tile.strip() or '\n' in tile for tile in self.bank)):
            raise ValueError("Each block needs distinct, readable text")
        if normalized(self.dutch) != normalized(" ".join(self.tiles)):
            raise ValueError("The source blocks do not reconstruct the Dutch sentence")
        if not re.search(rf"(?<!\w){re.escape(self.target_form.strip())}(?!\w)", self.dutch, re.I):
            raise ValueError("The saved vocabulary form is missing from the Dutch sentence")


class Drafts(practice.Content):
    """Return each proposed question with its selected vocabulary identity."""

    questions: list[Draft]


class Verdict(practice.Content):
    """Record the reviewer's blind reconstruction, including natural equivalent orders."""

    word_id: int
    valid: bool
    reason: str
    orders: list[list[int]] = pd.Field(max_length=24)

    def check(self, draft: Draft) -> None:
        """A decoy in any valid reconstruction disqualifies the exercise."""

        expected = list(range(len(draft.tiles)))
        orders = [tuple(order) for order in self.orders]
        if not self.valid or self.word_id != draft.word_id:
            raise ValueError(self.reason or "The independent review rejected the exercise")
        if tuple(expected) not in orders or any(sorted(order) != expected for order in orders):
            raise ValueError("The reviewer did not independently reconstruct the required blocks")


class Verdicts(practice.Content):
    """Keep a separate independent verdict per vocabulary target."""

    verdicts: list[Verdict]


class Grade(pd.BaseModel):
    """Preserve feedback for each actual submission, e.g. two extra or missing blocks."""

    correct: bool
    partial: float
    solution: list[int]
    omitted: list[int]
    excluded: list[int]


def normalized(text: str) -> str:
    """Compare words without capitalization or punctuation cues at the ends of tiles."""

    return " ".join(re.findall(r"\w+", text.casefold()))


def grade(draft: Draft, verdict: Verdict, selected: list[int]) -> Grade:
    """Choose the closest approved ordering and score the relative order of the blocks."""

    if (not selected or len(selected) != len(set(selected))
            or any(type(idx) is not int or not 0 <= idx < len(draft.bank) for idx in selected)):
        raise ValueError("The submitted blocks are missing or invalid")
    # Use LCS first so the animation preserves the largest possible ordered subsequence.
    solution = min(verdict.orders, key=lambda order: (-LCSseq.similarity(selected, order),
                                                     Levenshtein.distance(selected, order), order))
    return Grade(correct=selected == solution, partial=LCSseq.normalized_similarity(selected, solution),
                 solution=solution, omitted=[idx for idx in solution if idx not in selected],
                 excluded=[idx for idx in selected if idx not in solution])
