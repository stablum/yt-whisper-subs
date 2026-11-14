"""Validated practice content and FSRS review policy, independent of the native UI.

Example: `ReviewPolicy().answer(card_json, correct=True, unsure=False, now=now, elapsed_ms=800)`.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone

import fsrs
import pydantic as pd

from yt_whisper_subs import vocabulary


class Direction(enum.StrEnum):
    """Keep recognition and reverse recall separate; e.g. Dutch → English has its own schedule."""

    NL_EN = "nl_en"
    EN_NL = "en_nl"

    @property
    def label(self) -> str:
        """Name the round without exposing storage codes; e.g. Dutch → English."""

        return "Dutch → English" if self == self.NL_EN else "English → Dutch"


class Content(pd.BaseModel):
    """Reject unexpected or coerced AI fields; e.g. a string is never accepted as a verdict."""

    model_config = pd.ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class Distractor(Content):
    """Explain a plausible but incorrect option; e.g. a false friend versus the intended sense."""

    text: str = pd.Field(min_length=1, max_length=140)
    explanation: str = pd.Field(min_length=1, max_length=400)
    kind: str = pd.Field(pattern="^(false_friend|near_meaning|grammar|other)$")


class Draft(Content):
    """Let AI supply distractors while the saved vocabulary owns the correct answer."""

    lookup_id: int
    distractors: list[Distractor] = pd.Field(min_length=2, max_length=2)
    explanation: str = pd.Field(min_length=1, max_length=500)

    def check(self, answer: str) -> None:
        """Reject duplicate choices before semantic verification; e.g. capitalization cannot hide a duplicate."""

        texts = [answer, *(item.text for item in self.distractors)]
        normalized = [vocabulary.normalized(" ".join(text.split())).strip(" .!?") for text in texts]
        if len(set(normalized)) != 3 or any("\n" in text or "\r" in text for text in texts):
            raise ValueError("Practice choices must be three distinct, single-line answers")


class Drafts(Content):
    """Validate a bounded generation batch; e.g. six vocabulary terms in one request."""

    questions: list[Draft]


class Verdict(Content):
    """Keep semantic approval separate from question generation; e.g. reject two valid synonyms."""

    lookup_id: int
    valid: bool
    reason: str


class Verdicts(Content):
    """Match independent quality checks to exact vocabulary IDs; e.g. no missing verdicts."""

    verdicts: list[Verdict]


class RoundOptions(Content):
    """Bound a configurable round; e.g. ten distinct terms with optional early review."""

    direction: Direction
    count: int = pd.Field(ge=1, le=50)
    include_early: bool = False


class ReviewPolicy:
    """Use maintained FSRS scheduling with conservative recognition ratings; e.g. a lucky guess is Hard."""

    def __init__(self, document: str | None = None) -> None:
        """Persist the exact policy with each round; e.g. future analysis can reproduce its schedules."""

        self._scheduler = fsrs.Scheduler.from_json(document) if document else fsrs.Scheduler(enable_fuzzing=False)

    def document(self) -> str:
        """Serialize model parameters and learning steps; e.g. keep a round auditable."""

        return self._scheduler.to_json()

    @staticmethod
    def new_card(card_id: int, now: float) -> str:
        """Give review logs a stable distinct skill ID; e.g. future fitting never merges two simultaneous cards."""

        return fsrs.Card(card_id=card_id, due=datetime.fromtimestamp(now, timezone.utc)).to_json()

    def recall(self, card_json: str | None, now: float) -> float:
        """Rank due reviews by FSRS retrievability R; e.g. prioritize a fragile remembered word."""

        if not card_json:
            return 0.0
        card = fsrs.Card.from_json(card_json)
        return self._scheduler.get_card_retrievability(card, datetime.fromtimestamp(now, timezone.utc))

    def answer(self, card_json: str | None, *, correct: bool, unsure: bool, now: float, elapsed_ms: int) -> dict:
        """Map outcome to Again/Hard/Good, never speed-based Easy; e.g. interruptions do not lower mastery."""

        instant = datetime.fromtimestamp(now, timezone.utc)
        card = fsrs.Card.from_json(card_json) if card_json else fsrs.Card(due=instant)
        before = card.to_json()
        rating = fsrs.Rating.Again if not correct else (fsrs.Rating.Hard if unsure else fsrs.Rating.Good)
        recall = self._scheduler.get_card_retrievability(card, instant)
        card, log = self._scheduler.review_card(card, rating, instant, elapsed_ms)
        return {"before_json": before, "after_json": card.to_json(), "rating": int(rating),
                "recall": recall, "log_json": log.to_json(), "due_at": card.due.timestamp()}
