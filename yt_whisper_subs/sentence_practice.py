"""Sentence translation contracts with separate vocabulary meaning and inflection credit.

Example: a wrong superlative can fail form recall while preserving credit for the adjective's meaning.
"""

from __future__ import annotations

import enum
import re
from typing import Literal

import pydantic as pd

from yt_whisper_subs import i18n
from yt_whisper_subs import practice
from yt_whisper_subs import verb_practice
from yt_whisper_subs import vocabulary_learning as learning


SCENES = {"home": i18n.mark('Home and family'), "shopping": i18n.mark('Shopping'), "work": i18n.mark('Work and study'),
          "travel": i18n.mark('Travel and directions'), "food": i18n.mark('Food and cafés'), "appointments": i18n.mark('Appointments and services'),
          "social": i18n.mark('Conversations and plans'), "hobbies": i18n.mark('Hobbies and free time'), "public_life": i18n.mark('News and public life')}
FORMS = {"base": i18n.mark('Base form'), "person": i18n.mark('Subject and person'), "present": i18n.mark('Present tense'), "past": i18n.mark('Past tense'),
         "perfect": i18n.mark('Perfect tenses'), "future": i18n.mark('Future'), "conditional": i18n.mark('Conditionals'), "subjunctive": i18n.mark('Subjunctive'),
         "imperative": i18n.mark('Imperative'), "singular": i18n.mark('Singular'), "plural": i18n.mark('Plural'), "comparative": i18n.mark('Comparatives'),
         "superlative": i18n.mark('Superlatives'), "adjective_ending": i18n.mark('Adjective endings'), "possessive": i18n.mark('Possessives'),
         "diminutive": i18n.mark('Diminutives')}
Form = Literal["base", "person", "present", "past", "perfect", "future", "conditional", "subjunctive",
               "imperative", "singular", "plural", "comparative", "superlative", "adjective_ending", "possessive", "diminutive"]
ErrorKind = Literal["meaning", "omission", "extra_content", "word_order", "verb_form", "agreement", "article",
                    "preposition", "spelling", "degree", "number", "idiom", "transcription", "other"]


class Direction(str, enum.Enum):
    """Separate dictation from translation skills; e.g. hearing Dutch does not count as English-to-Dutch writing."""

    EN_NL = "en_nl"
    NL_EN = "nl_en"
    LISTEN = "listen_nl"

    @property
    def label(self) -> str:
        """Name the actual exercise; e.g. dictation is heard Dutch → written Dutch."""

        return i18n.tr('Dutch dictation') if self == self.LISTEN else practice.Direction(self.value).label

    @property
    def word_direction(self) -> practice.Direction:
        """Share recognition priorities for listening; e.g. a missed spoken target also returns in Dutch word recognition."""

        return practice.Direction.NL_EN if self == self.LISTEN else practice.Direction(self.value)


class Options(pd.BaseModel):
    """Configure whole-sentence recall; e.g. ten English-to-Dutch questions with two saved target words each."""

    model_config = pd.ConfigDict(extra="forbid")
    direction: Direction = Direction.EN_NL
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


class MeaningLink(practice.Content):
    """Align exact fragments of one idea; e.g. 'would walk' can match 'zou' and 'lopen'."""

    dutch: list[str] = pd.Field(min_length=1)
    english: list[str] = pd.Field(min_length=1)


class AuditDecision(Decision):
    """Require evidence beyond the saved words; e.g. 'from the menu' needs a Dutch counterpart."""

    links: list[MeaningLink]

    def check(self, draft: Draft) -> None:
        """Check that accepted links exist and cover both texts; e.g. a fabricated counterpart cannot pass."""

        for language in ("dutch", "english"):
            text = getattr(draft, language)
            covered = set()
            for link in self.links:
                for fragment in getattr(link, language):
                    fragment = fragment.strip()
                    if not re.search(r"\w", fragment):
                        raise ValueError(f"A {language.capitalize()} audit fragment contains no words.")
                    pattern = rf"(?<!\w){re.escape(fragment)}(?!\w)"
                    matches = list(re.finditer(pattern, text, re.IGNORECASE))
                    if not matches:
                        raise ValueError(f"The {language.capitalize()} audit fragment {fragment!r} is absent from its sentence.")
                    for match in matches:
                        covered.update(range(match.start(), match.end()))
            missing = [word.group() for word in re.finditer(r"\w+", text) if word.start() not in covered]
            if missing:
                raise ValueError(f"{language.capitalize()} details without a matched counterpart: {' '.join(missing)}.")


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
    source_forms: list[str] = pd.Field(max_length=8)


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

    def check(self, answer: str, content: dict, direction: str) -> None:
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
        source = content["english" if direction == "en_nl" else "dutch"]
        for error in self.errors:
            if any(not form.strip() or form not in source for form in error.source_forms):
                raise ValueError("Correction evidence was absent from the original source sentence")
            if error.evidence and error.evidence.casefold() not in answer.casefold():
                raise ValueError("Correction evidence was absent from the submitted sentence")
            if len(error.word_ids) != len(set(error.word_ids)) or not set(error.word_ids) <= set(words):
                raise ValueError("An error was attributed to unknown vocabulary")
            conflicts = [word_id for word_id in error.word_ids if words[word_id].correct]
            if conflicts:
                raise ValueError(f"An error contradicts its word assessment for word IDs {conflicts}: {error.kind} ({error.evidence!r})")
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
