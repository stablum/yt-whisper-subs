"""Sentence translation contracts with separate vocabulary meaning and inflection credit.

Example: a wrong superlative can fail form recall while preserving credit for the adjective's meaning.
"""

from __future__ import annotations

from typing import Literal

import pydantic as pd

from yt_whisper_subs import practice
from yt_whisper_subs import verb_practice
from yt_whisper_subs import vocabulary_learning as learning


SCENES = {"home": "Home and family", "shopping": "Shopping", "work": "Work and study",
          "travel": "Travel and directions", "food": "Food and cafés", "appointments": "Appointments and services",
          "social": "Conversations and plans", "hobbies": "Hobbies and free time", "public_life": "News and public life"}
FORMS = {"base": "Base form", "person": "Subject and person", "present": "Present tense", "past": "Past tense",
         "perfect": "Perfect tenses", "future": "Future", "conditional": "Conditionals", "subjunctive": "Subjunctive",
         "imperative": "Imperative", "singular": "Singular", "plural": "Plural", "comparative": "Comparatives",
         "superlative": "Superlatives", "adjective_ending": "Adjective endings", "possessive": "Possessives",
         "diminutive": "Diminutives"}
Form = Literal["base", "person", "present", "past", "perfect", "future", "conditional", "subjunctive",
               "imperative", "singular", "plural", "comparative", "superlative", "adjective_ending", "possessive", "diminutive"]
ErrorKind = Literal["meaning", "omission", "extra_content", "word_order", "verb_form", "agreement", "article",
                    "preposition", "spelling", "degree", "number", "idiom", "other"]


class Options(pd.BaseModel):
    """Configure whole-sentence recall; e.g. ten English-to-Dutch questions with two saved target words each."""

    model_config = pd.ConfigDict(extra="forbid")
    direction: practice.Direction = practice.Direction.EN_NL
    count: int = pd.Field(default=10, ge=1, le=50)
    words: int = pd.Field(default=2, ge=1, le=3)
    scene: str = "mixed"
    include_early: bool = False

    @pd.field_validator("scene")
    @classmethod
    def scene_exists(cls, value: str) -> str:
        """Reject unsupported contexts before network calls; e.g. a saved typo cannot invent a scenario."""

        if value not in {"mixed", *SCENES}:
            raise ValueError("Unknown sentence context")
        return value


class WordUse(practice.Content):
    """Ground an inflected target in both sentences; e.g. liep becomes zullen lopen without changing its saved meaning."""

    word_id: int
    dutch_forms: list[str] = pd.Field(min_length=1, max_length=5)
    english_forms: list[str] = pd.Field(min_length=1, max_length=5)
    features: list[Form] = pd.Field(min_length=1, max_length=5)
    explanation: str = pd.Field(min_length=3, max_length=700)


class Draft(practice.Content):
    """Pair natural sentences around saved learning targets; e.g. supporting vocabulary is freely allowed."""

    anchor_word_id: int
    scene: str
    dutch: str = pd.Field(min_length=3, max_length=600)
    english: str = pd.Field(min_length=3, max_length=700)
    words: list[WordUse] = pd.Field(min_length=1, max_length=3)
    alternatives: list[str] = pd.Field(max_length=6)
    explanation: str = pd.Field(min_length=3, max_length=1200)

    def check(self, expected: set[int], scene: str) -> None:
        """Validate IDs and exact evidence before linguistic review; e.g. a separated particle must really occur."""

        ids = [row.word_id for row in self.words]
        if len(ids) != len(set(ids)) or set(ids) != expected or self.anchor_word_id not in expected:
            raise ValueError("Sentence targets were missing, duplicated or substituted")
        if self.scene not in SCENES or (scene != "mixed" and self.scene != scene):
            raise ValueError("The sentence changed its requested life context")
        for row in self.words:
            for language, text, forms in (("Dutch", self.dutch, row.dutch_forms), ("English", self.english, row.english_forms)):
                for form in forms:
                    if not form.strip() or form.casefold() not in text.casefold():
                        raise ValueError(f"The {language} evidence {form!r} for word {row.word_id} is absent from its sentence; use exact substrings.")
            if len(row.features) != len(set(row.features)):
                raise ValueError("Duplicate word-form targets")
        if any(not text.strip() or len(text) > 1000 for text in self.alternatives):
            raise ValueError("Invalid accepted sentence alternative")


class Drafts(practice.Content):
    """Bound a generation batch by locally chosen anchors; e.g. four complete bilingual pairs at a time."""

    questions: list[Draft]


class Decision(practice.Content):
    """Audit content independently of identifier bookkeeping; e.g. each required question slot receives a real decision."""

    valid: bool
    reason: str


class Verdict(Decision):
    """Bind an audited decision to its local anchor; e.g. storage never relies on the model copying the right ID."""

    anchor_word_id: int


class FormResult(practice.Content):
    """Track a specific inflection when assessable; e.g. an omitted word supplies no evidence about its plural ending."""

    feature: Form
    correct: bool | None
    explanation: str = pd.Field(min_length=1, max_length=500)


class WordResult(practice.Content):
    """Keep lexical understanding distinct from form accuracy; e.g. slimmer shows the concept but misses a required superlative."""

    word_id: int
    meaning_correct: bool
    target_used: bool
    form_correct: bool | None
    explanation: str = pd.Field(min_length=1, max_length=700)
    forms: list[FormResult] = pd.Field(min_length=1, max_length=5)

    @property
    def correct(self) -> bool:
        """Rate the saved word independently of unrelated grammar; e.g. a global word-order error need not fail all targets."""

        return self.meaning_correct and self.form_correct is not False


class Error(practice.Content):
    """Attach a concrete correction only to affected targets; e.g. a supporting-word mistake can have no saved word IDs."""

    kind: ErrorKind
    word_ids: list[int] = pd.Field(max_length=3)
    evidence: str = pd.Field(max_length=2000)
    correction: str = pd.Field(min_length=1, max_length=1000)
    explanation: str = pd.Field(min_length=3, max_length=900)


class Grade(practice.Content):
    """Persist whole-sentence correctness plus diagnostic word/form evidence; e.g. uncertain judgments stay pending."""

    confident: bool
    correct: bool
    corrected: str = pd.Field(min_length=3, max_length=1000)
    corrected_meaning: str = pd.Field(min_length=3, max_length=1000)
    explanation: str = pd.Field(min_length=3, max_length=1500)
    words: list[WordResult] = pd.Field(min_length=1, max_length=3)
    errors: list[Error] = pd.Field(max_length=15)
    vocabulary: list[learning.Correction] = pd.Field(max_length=10)

    def check(self, answer: str, content: dict) -> None:
        """Reject fabricated or inconsistent diagnostics; e.g. a whole-sentence failure cannot blindly punish unrelated words."""

        expected = {row["word_id"]: set(row["features"]) for row in content["words"]}
        words = {row.word_id: row for row in self.words}
        if len(words) != len(self.words) or set(words) != set(expected):
            raise ValueError("The grader omitted or substituted target words")
        if self.correct != (not self.errors and all(row.correct for row in self.words)):
            raise ValueError("Contradictory sentence and vocabulary grading")
        if self.correct != (verb_practice.normalized(self.corrected) == verb_practice.normalized(answer)):
            raise ValueError("Correction contradicts the punctuation and spelling policy")
        affected = set()
        for error in self.errors:
            if error.evidence and error.evidence.casefold() not in answer.casefold():
                raise ValueError("Correction evidence was absent from the submitted sentence")
            if len(error.word_ids) != len(set(error.word_ids)) or not set(error.word_ids) <= set(words):
                raise ValueError("An error was attributed to unknown vocabulary")
            if any(words[word_id].correct for word_id in error.word_ids):
                raise ValueError("An error contradicts its word assessment")
            affected.update(error.word_ids)
        for word in self.words:
            features = {row.feature for row in word.forms}
            if len(features) != len(word.forms) or features != expected[word.word_id]:
                raise ValueError("A grammatical form was omitted or invented in grading")
            if any(row.correct is False for row in word.forms) and word.form_correct is not False:
                raise ValueError("An inflection result contradicts its word grade")
            if not word.correct and word.word_id not in affected:
                raise ValueError("Each failed target needs a specific correction")
        if not self.confident:
            raise ValueError("The correction was uncertain. Retry or report the exercise; no failure was recorded.")
