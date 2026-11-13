"""Dutch transformation contracts and a constrained grammar syllabus.

Example: `Options(focus="conditionals")` targets hypothetical and counterfactual sentences.
"""

from __future__ import annotations

import enum
import re
import unicodedata
from typing import Literal

import pydantic as pd

from yt_whisper_subs import practice


class Tense(enum.StrEnum):
    """Keep tense and mood targets explicit; e.g. subjunctives have their own review history."""

    PRESENT = "present"
    PAST = "past"
    PERFECT = "perfect"
    PLUPERFECT = "pluperfect"
    FUTURE = "future"
    FUTURE_PERFECT = "future_perfect"
    CONDITIONAL = "conditional"
    CONDITIONAL_PERFECT = "conditional_perfect"
    SUBJUNCTIVE = "subjunctive"
    SUBJUNCTIVE_PAST = "subjunctive_past"
    IMPERATIVE = "imperative"

    @property
    def label(self) -> str:
        """Explain the requested form in learner language; e.g. Past perfect · voltooid verleden tijd."""

        return TENSE_LABELS[self.value]


TENSE_LABELS = {
    "present": "Present · tegenwoordige tijd",
    "past": "Simple past · onvoltooid verleden tijd",
    "perfect": "Present perfect · voltooid tegenwoordige tijd",
    "pluperfect": "Past perfect · voltooid verleden tijd",
    "future": "Future · zullen + infinitive",
    "future_perfect": "Future perfect · zullen + perfect infinitive",
    "conditional": "Conditional · zouden + infinitive",
    "conditional_perfect": "Conditional perfect · zouden + perfect infinitive",
    "subjunctive": "Present subjunctive · aanvoegende wijs",
    "subjunctive_past": "Past subjunctive · ware",
    "imperative": "Imperative · gebiedende wijs",
}
SUBJECTS = {"ik": "ik", "jij": "jij", "u": "u (polite)", "hij": "hij",
            "zij_sg": "zij (she)", "het": "het", "wij": "wij", "jullie": "jullie",
            "zij_pl": "zij (they)", "men": "men", "de_koning": "de koning (third-person singular)",
            "de_jarigen": "de jarigen (third-person plural)"}
STRUCTURES = {"statement": "Statement", "question": "Yes/no question · inversion",
              "subordinate": "Subordinate clause · begin with ‘omdat’",
              "negative": "Negative sentence", "passive": "Passive voice",
              "if_clause": "Hypothetical ‘als’ clause + result",
              "wish": "Formal wish or fixed expression", "formal": "Formal unreal comparison or hypothesis",
              "command": "Direct command"}
FOCUSES = {"all": "All forms · adaptive", "everyday": "Everyday tenses",
           "conditionals": "Conditionals · present and perfect",
           "subjunctives": "Subjunctives · present and past",
           "imperatives": "Imperatives"}
AXES = ("tense", "subject", "structure", "verb")
AxisName = Literal["tense", "subject", "structure", "verb"]
ErrorKind = Literal["tense", "agreement", "verb_spelling", "auxiliary", "participle", "infinitive",
                    "word_order", "separable_prefix", "reflexive", "negation", "mood", "meaning", "other"]


class Options(pd.BaseModel):
    """Configure a bounded adaptive round; e.g. isolate jij inversion or practise all conditionals."""

    model_config = pd.ConfigDict(extra="forbid")
    count: int = pd.Field(default=10, ge=1, le=50)
    focus: str = "all"
    subject: str = "all"
    structure: str = "all"
    include_early: bool = False

    @pd.model_validator(mode="after")
    def check(self) -> Options:
        """Reject unknown filters before any API work; e.g. malformed saved options cannot change the syllabus."""

        if self.focus not in {*FOCUSES, *TENSE_LABELS} or self.subject not in {"all", *SUBJECTS}:
            raise ValueError("Unknown verb focus or subject")
        if self.structure not in {"all", *STRUCTURES}:
            raise ValueError("Unknown sentence structure")
        return self


class Draft(practice.Content):
    """Store a complete sentence transformation; e.g. the answer stays hidden until submission."""

    target_id: int
    source: str = pd.Field(min_length=3, max_length=500)
    instruction: str = pd.Field(min_length=5, max_length=700)
    answer: str = pd.Field(min_length=3, max_length=500)
    alternatives: list[str] = pd.Field(max_length=8)
    explanation: str = pd.Field(min_length=5, max_length=1000)

    @pd.model_validator(mode="after")
    def changed(self) -> Draft:
        """Require an actual rewrite; e.g. the source cannot already be the canonical answer."""

        if normalized(self.source) == normalized(self.answer):
            raise ValueError("The transformation must change the source sentence")
        if any(not item.strip() or len(item) > 500 for item in self.alternatives):
            raise ValueError("Invalid accepted alternative")
        return self


class Drafts(practice.Content):
    """Bound a generation batch; e.g. generate four planned targets at once."""

    questions: list[Draft]


class Verdict(practice.Content):
    """Record independent linguistic approval; e.g. reject an unnatural invented subjunctive."""

    target_id: int
    valid: bool
    reason: str


class Verdicts(practice.Content):
    """Match quality checks to local targets; e.g. no extra generated IDs are trusted."""

    verdicts: list[Verdict]


class Axis(practice.Content):
    """Assess each tested dimension independently; e.g. a spelling error need not be a tense error."""

    axis: AxisName
    correct: bool


class Error(practice.Content):
    """Preserve concrete correction evidence; e.g. ‘jij loop’ needs ‘jij loopt’."""

    kind: ErrorKind
    axis: AxisName
    evidence: str = pd.Field(max_length=500)
    correction: str = pd.Field(min_length=1, max_length=500)
    explanation: str = pd.Field(min_length=3, max_length=700)


class Grade(practice.Content):
    """Separate confidence, correctness and diagnostic evidence; e.g. uncertain judgments do not train the scheduler."""

    confident: bool
    correct: bool
    corrected: str = pd.Field(min_length=3, max_length=500)
    explanation: str = pd.Field(min_length=3, max_length=1200)
    axes: list[Axis] = pd.Field(min_length=4, max_length=4)
    errors: list[Error] = pd.Field(max_length=12)

    def check(self, answer: str) -> None:
        """Reject contradictory or fabricated feedback; e.g. an error quote must appear in the submitted sentence."""

        axes = {item.axis: item.correct for item in self.axes}
        if len(axes) != 4 or set(axes) != set(AXES):
            raise ValueError("The grader omitted a tested dimension")
        if self.correct != (all(axes.values()) and not self.errors):
            raise ValueError("The grader returned contradictory feedback")
        if not self.correct and not self.errors:
            raise ValueError("An incorrect answer needs a concrete explanation")
        unchanged = normalized(self.corrected) == normalized(answer)
        if self.correct != unchanged:
            raise ValueError("The correction contradicts the spelling and punctuation grading policy")
        if any(not correct and axis not in {item.axis for item in self.errors} for axis, correct in axes.items()):
            raise ValueError("Every failed dimension needs a concrete error")
        for error in self.errors:
            if error.evidence and error.evidence.casefold() not in answer.casefold():
                raise ValueError("The grader quoted text absent from your answer")
            if axes[error.axis]:
                raise ValueError("An error contradicts its dimension assessment")
        if not self.confident:
            raise ValueError("The correction was uncertain. Retry checking or report this question; no failure was recorded.")


def normalized(text: str) -> str:
    """Ignore case and harmless sentence punctuation, preserving spelling; e.g. ‘loopt’ never equals ‘loop’."""

    text = unicodedata.normalize("NFC", text).casefold()
    return " ".join(re.sub(r'[.,!?;:“”"„]', "", text).split())


def target_label(row: dict) -> str:
    """Present locally owned requirements; e.g. subject, tense and inversion cannot be silently changed by AI."""

    return f"{SUBJECTS[row['subject']]}  ·  {TENSE_LABELS[row['tense']]}  ·  {STRUCTURES[row['structure']]}"
