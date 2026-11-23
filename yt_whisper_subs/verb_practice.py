"""Dutch transformation contracts and a constrained grammar syllabus.

Example: `Options(focus="conditionals")` targets hypothetical and counterfactual sentences.
"""

from __future__ import annotations

import enum
import re
import unicodedata
from typing import ClassVar, Literal

import pydantic as pd

from yt_whisper_subs import i18n
from yt_whisper_subs import practice
from yt_whisper_subs import vocabulary_learning as learning


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

        return i18n.tr(TENSE_LABELS[self.value])


TENSE_LABELS = {
    "present": i18n.mark('Present · tegenwoordige tijd'),
    "past": i18n.mark('Simple past · onvoltooid verleden tijd'),
    "perfect": i18n.mark('Present perfect · voltooid tegenwoordige tijd'),
    "pluperfect": i18n.mark('Past perfect · voltooid verleden tijd'),
    "future": i18n.mark('Future · zullen + infinitive'),
    "future_perfect": i18n.mark('Future perfect · zullen + perfect infinitive'),
    "conditional": i18n.mark('Conditional · zouden + infinitive'),
    "conditional_perfect": i18n.mark('Conditional perfect · zouden + perfect infinitive'),
    "subjunctive": i18n.mark('Present subjunctive · aanvoegende wijs'),
    "subjunctive_past": i18n.mark('Past subjunctive · ware'),
    "imperative": i18n.mark('Imperative · gebiedende wijs'),
}
SUBJECTS = {"ik": "ik", "jij": "jij", "u": i18n.mark('u (polite)'), "hij": "hij",
            "zij_sg": i18n.mark('zij (she)'), "het": "het", "wij": "wij", "jullie": "jullie",
            "zij_pl": i18n.mark('zij (they)'), "men": "men", "de_koning": i18n.mark('de koning (third-person singular)'),
            "de_jarigen": i18n.mark('de jarigen (third-person plural)')}
STRUCTURES = {"statement": i18n.mark('Statement'), "question": i18n.mark('Yes/no question · inversion'),
              "subordinate": i18n.mark('Subordinate clause · begin with ‘omdat’'),
              "negative": i18n.mark('Negative sentence'), "passive": i18n.mark('Passive voice'),
              "if_clause": i18n.mark('Hypothetical ‘als’ clause + result'),
              "wish": i18n.mark('Formal wish or fixed expression'), "formal": i18n.mark('Formal unreal comparison or hypothesis'),
              "command": i18n.mark('Direct command')}
FOCUSES = {"all": i18n.mark('All forms · adaptive'), "everyday": i18n.mark('Everyday tenses'),
           "conditionals": i18n.mark('Conditionals · present and perfect'),
           "subjunctives": i18n.mark('Subjunctives · present and past'),
           "imperatives": i18n.mark('Imperatives')}
SOURCES = {"all": i18n.mark('My vocabulary + starter verbs'), "vocabulary": i18n.mark('My vocabulary only'), "starter": i18n.mark('Starter verbs only')}
AXES = ("tense", "subject", "structure", "verb")
AxisName = Literal["tense", "subject", "structure", "verb"]
ErrorKind = Literal["tense", "agreement", "verb_spelling", "auxiliary", "participle", "infinitive",
                    "word_order", "separable_prefix", "reflexive", "negation", "mood", "meaning", "other"]


class Options(pd.BaseModel):
    """Configure a bounded adaptive round; e.g. isolate jij inversion or practise all conditionals."""

    model_config = pd.ConfigDict(extra="forbid")
    variant: ClassVar[str] = ""
    focuses: ClassVar[dict[str, str]] = {**FOCUSES, **TENSE_LABELS}
    subjects: ClassVar[dict[str, str]] = SUBJECTS
    structures: ClassVar[dict[str, str]] = STRUCTURES
    count: int = pd.Field(default=10, ge=1, le=50)
    focus: str = "all"
    subject: str = "all"
    structure: str = "all"
    source: str = "all"
    include_early: bool = False

    @pd.model_validator(mode="after")
    def check(self) -> Options:
        """Reject unknown filters before any API work; e.g. malformed saved options cannot change the syllabus."""

        if self.focus not in self.focuses or self.subject not in {"all", *self.subjects}:
            raise ValueError("Unknown verb focus or subject")
        if self.structure not in {"all", *self.structures}:
            raise ValueError("Unknown sentence structure")
        if self.source not in SOURCES:
            raise ValueError("Unknown verb source")
        return self


class Draft(practice.Content):
    """Store a complete sentence transformation; e.g. the answer stays hidden until submission."""

    target_id: int
    source: str = pd.Field(min_length=3, max_length=500)
    source_english: str = pd.Field(min_length=3, max_length=700)
    instruction: str = pd.Field(min_length=5, max_length=700)
    answer: str = pd.Field(min_length=3, max_length=500)
    answer_english: str = pd.Field(min_length=3, max_length=700)
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


class LexemeProposal(practice.Content):
    """Parse each extraction's fields before checking their coherence."""

    lookup_id: int
    is_verb: bool
    lemma: str = pd.Field(max_length=120)
    family: Literal["regular", "strong", "irregular", "separable", "reflexive", "modal"] | None
    transitive: bool | None
    reason: str = pd.Field(min_length=3, max_length=700)


class Lexeme(LexemeProposal):
    """Normalize a saved occurrence into validated lexical facts; e.g. vergiste becomes zich vergissen."""

    @pd.model_validator(mode="after")
    def check(self) -> Lexeme:
        """Require one usable infinitive for an accepted verb; e.g. noun homonyms remain excluded."""

        self.lemma = " ".join(unicodedata.normalize("NFC", self.lemma).casefold().split())
        words = self.lemma.split()
        if self.is_verb:
            valid = len(words) == 1 or (len(words) == 2 and words[0] == "zich")
            if not valid or not any(char.isalpha() for char in self.lemma) or self.family is None or self.transitive is None:
                raise ValueError("An accepted verb needs a dictionary infinitive and lexical metadata")
            if self.family == "reflexive" and not self.lemma.startswith("zich "):
                raise ValueError("Reflexive lemmas must include zich")
        elif self.lemma or self.family is not None or self.transitive is not None:
            raise ValueError("Rejected occurrences cannot supply a verb")
        return self


class Lexemes(practice.Content):
    """Keep extraction batches bounded and ID-addressed; e.g. several clicked forms may identify one infinitive."""

    verbs: list[LexemeProposal]


class LexemeCheck(practice.Content):
    """Independently audit lexical mapping and metadata; e.g. a noun reading cannot seed verb practice."""

    lookup_id: int
    valid: bool
    reason: str = pd.Field(min_length=3, max_length=700)


class LexemeChecks(practice.Content):
    """Require a quality decision for every occurrence; e.g. missing audits are never treated as approval."""

    verdicts: list[LexemeCheck]


class SourceEnglish(practice.Content):
    """Translate only the original source sentence; e.g. help never reveals the requested transformed answer."""

    english: str = pd.Field(min_length=3, max_length=1000)


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
    corrected_english: str = pd.Field(min_length=3, max_length=1000)
    explanation: str = pd.Field(min_length=3, max_length=1200)
    axes: list[Axis] = pd.Field(min_length=4, max_length=4)
    errors: list[Error] = pd.Field(max_length=12)
    vocabulary: list[learning.Correction] = pd.Field(max_length=10)

    @property
    def grammar_correct(self) -> bool | None:
        """Rate the transformation separately, e.g. a word substitution alone may leave the target unassessed."""

        if any(error.kind != "meaning" for error in self.errors):
            return False
        return True if all(item.correct for item in self.axes) else None

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
            if axes[error.axis] and error.kind != "meaning":
                raise ValueError("An error contradicts its dimension assessment")
        if not self.confident:
            raise ValueError("The correction was uncertain. Retry checking or report this question; no failure was recorded.")


def normalized(text: str) -> str:
    """Ignore case and harmless sentence punctuation, preserving spelling; e.g. ‘loopt’ never equals ‘loop’."""

    text = unicodedata.normalize("NFC", text).casefold()
    return " ".join(re.sub(r'[.,!?;:“”"„]', "", text).split())


def target_label(row: dict) -> str:
    """Present locally owned requirements; e.g. subject, tense and inversion cannot be silently changed by AI."""

    return f"{i18n.tr(SUBJECTS[row['subject']])}  ·  {i18n.tr(TENSE_LABELS[row['tense']])}  ·  {i18n.tr(STRUCTURES[row['structure']])}"
