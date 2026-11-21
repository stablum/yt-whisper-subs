"""Define grounded Dutch passage exercises and criterion-based feedback.

Example: reading and listening share an audited story but keep independent attempts and progress.
"""

import enum
import json
import re
from typing import Literal

import pydantic as pd

from yt_whisper_subs import i18n
from yt_whisper_subs import practice
from yt_whisper_subs import vocabulary_learning as learning


class Direction(str, enum.Enum):
    """Name the two input modalities; e.g. listening never requires translating into English."""

    READ = "read"
    LISTEN = "listen"

    @property
    def label(self) -> str:
        """Name the modality in the UI language; e.g. Dutch uses Leesbegrip beside Luisterbegrip."""

        return i18n.tr('Reading comprehension') if self == self.READ else i18n.tr('Listening comprehension')


class Level(enum.StrEnum):
    """Share the available CEFR levels across saved options and the UI; e.g. A1 is the first choice."""

    A1 = 'A1'
    A2 = 'A2'
    B1 = 'B1'
    B2 = 'B2'
    C1 = 'C1'

    @property
    def min_paragraph_words(self) -> int:
        """Allow very short A1 paragraphs; e.g. two short sentences can form a paragraph."""

        return 10 if self == self.A1 else 20


GENRES = {"mixed": i18n.mark('Mixed'), "story": i18n.mark('Story'), "article": i18n.mark('Fictional news article'), "essay": i18n.mark('Reflective essay')}
SKILLS = {"detail": i18n.mark('Details'), "inference": i18n.mark('Inferences'), "cause": i18n.mark('Cause and effect'),
          "argument": i18n.mark('Arguments'), "tone": i18n.mark('Tone and intent'), "reference": i18n.mark('Connections and references')}


class Options(pd.BaseModel):
    """Configure content independently from narration; e.g. five choices and three open answers accompany one passage."""

    direction: Direction = Direction.READ
    genre: Literal["mixed", "story", "article", "essay"] = "mixed"
    level: Level = Level.B2
    paragraphs: int = pd.Field(default=5, ge=2, le=10)
    mc: int = pd.Field(default=5, ge=0, le=20)
    open: int = pd.Field(default=3, ge=0, le=15)
    words: int = pd.Field(default=8, ge=1, le=20)
    include_early: bool = False

    @pd.model_validator(mode="after")
    def questions_required(self):
        """Require a bounded real round; e.g. zero open questions is valid when multiple-choice questions remain."""

        if not 1 <= self.mc + self.open <= 30:
            raise ValueError("Kies samen 1–30 vragen.")
        return self

    @property
    def count(self) -> int:
        """Count actual questions rather than passages; e.g. 5 + 3 always presents eight questions."""

        return self.mc + self.open

    def content_key(self, *, include_counts: bool = True) -> str:
        """Reuse unused content across narration changes; e.g. changing a speaker spends no passage-generation tokens."""

        exclude = {"direction", "include_early"}
        if not include_counts:
            exclude.update(("mc", "open"))
        data = self.model_dump(mode="json", exclude=exclude)
        return json.dumps(data, sort_keys=True)


class Evidence(practice.Content):
    """Anchor a conclusion to exact source text; e.g. a distractor cannot be justified with outside knowledge."""

    paragraph: int = pd.Field(ge=1, le=10)
    quote: str = pd.Field(min_length=3, max_length=1200)


class Target(practice.Content):
    """Identify real inflected vocabulary evidence; e.g. veilig may appear as veiligere in the passage."""

    word_id: int
    forms: list[str] = pd.Field(min_length=1, max_length=6)


class Choice(practice.Content):
    """Retain a specific distractor rationale for later feedback; e.g. almost true still needs a concrete contradiction."""

    text: str = pd.Field(min_length=2, max_length=600)
    explanation: str = pd.Field(min_length=3, max_length=1000)


class Criterion(practice.Content):
    """Define fixed content credit before seeing the learner's answer; e.g. naming a cause and consequence gives two criteria."""

    key: str = pd.Field(min_length=1, max_length=50)
    text: str = pd.Field(min_length=3, max_length=600)


class Question(practice.Content):
    """Keep answer keys private until feedback; e.g. an inference question has exactly one defensible choice."""

    key: int = pd.Field(ge=1, le=30)
    kind: Literal["mc", "open"]
    skill: Literal["detail", "inference", "cause", "argument", "tone", "reference"]
    prompt: str = pd.Field(min_length=8, max_length=1200)
    choices: list[Choice] = pd.Field(max_length=4)
    correct_idx: int | None
    answer: str = pd.Field(min_length=2, max_length=1800)
    rubric: list[Criterion] = pd.Field(max_length=5)
    evidence: list[Evidence] = pd.Field(min_length=1, max_length=4)
    word_ids: list[int] = pd.Field(max_length=8)


class Passage(practice.Content):
    """Store a complete auditable exercise before media preparation; e.g. failed speech synthesis never discards paid text."""

    title: str = pd.Field(min_length=3, max_length=180)
    genre: Literal["story", "article", "essay"]
    paragraphs: list[str] = pd.Field(min_length=2, max_length=10)
    targets: list[Target] = pd.Field(min_length=1, max_length=20)
    questions: list[Question] = pd.Field(min_length=1, max_length=30)

    def check(self, opts: Options, target_ids: set[int]) -> None:
        """Reject omissions and unsupported evidence locally; e.g. requested counts cannot become a smaller round after audit."""

        if len(self.paragraphs) != opts.paragraphs or any(len(p.split()) < opts.level.min_paragraph_words for p in self.paragraphs):
            raise ValueError("Het aantal alinea's of de tekstlengte klopt niet.")
        if sum(q.kind == "mc" for q in self.questions) != opts.mc or sum(q.kind == "open" for q in self.questions) != opts.open:
            raise ValueError("Het aantal meerkeuzevragen en open vragen klopt niet.")
        if opts.genre != "mixed" and self.genre != opts.genre:
            raise ValueError("De tekstsoort klopt niet.")
        if {t.word_id for t in self.targets} != target_ids or len(self.targets) != len(target_ids):
            raise ValueError("De tekst mist geselecteerde woordenschat.")
        text = "\n\n".join(self.paragraphs)
        for target in self.targets:
            if any(not form.strip() or not re.search(r"(?<!\w)" + re.escape(form) + r"(?!\w)", text, re.I) for form in target.forms):
                raise ValueError("Een opgegeven woordvorm staat niet in de tekst.")
        if {q.key for q in self.questions} != set(range(1, opts.count + 1)):
            raise ValueError("Vraagnummers ontbreken of zijn dubbel.")
        for q in self.questions:
            if len(q.word_ids) != len(set(q.word_ids)) or set(q.word_ids) - target_ids:
                raise ValueError("Een vraag verwijst naar onbekende woordenschat.")
            for item in q.evidence:
                if item.paragraph > len(self.paragraphs) or item.quote not in self.paragraphs[item.paragraph - 1]:
                    raise ValueError("Een bewijsplaats staat niet in de genoemde alinea.")
            if q.kind == "mc":
                if len(q.choices) != 4 or q.correct_idx not in range(4) or q.rubric:
                    raise ValueError("Een meerkeuzevraag heeft vier keuzes en één antwoord nodig.")
                if len({c.text.casefold().strip() for c in q.choices}) != 4:
                    raise ValueError("Dubbele antwoordkeuzes.")
            elif q.choices or q.correct_idx is not None or not 1 <= len(q.rubric) <= 5:
                raise ValueError("Een open vraag heeft vaste beoordelingscriteria nodig.")
            if len({c.key for c in q.rubric}) != len(q.rubric):
                raise ValueError("Dubbele beoordelingscriteria.")


class AuditQuestion(practice.Content):
    """Require a blind independent answer and ambiguity check; e.g. disagreement with the proposed answer prevents publication."""

    key: int
    valid: bool
    answer_idx: int | None
    explanation: str


class DraftRejected(ValueError):
    """Carry quality feedback and repairable questions, e.g. two questions that give each other away."""

    def __init__(self, message: str, questions: tuple[AuditQuestion, ...] = ()) -> None:
        """Keep the reviewer notes available for a focused repair."""

        super().__init__(message)
        self.questions = questions


class Audit(practice.Content):
    """Approve every question explicitly; e.g. omitted verdicts are never treated as approval."""

    natural_dutch: bool
    coherent: bool
    level_issue: str
    questions: list[AuditQuestion]

    def check(self, passage: Passage) -> None:
        """Compare independent decisions with the complete draft; e.g. two defensible choices require a repair."""

        expected = {q.key: q.correct_idx for q in passage.questions}
        if {item.key for item in self.questions} != expected.keys() or len(self.questions) != len(expected):
            raise ValueError("De onafhankelijke tekstcontrole is niet volledig geslaagd.")
        if not self.natural_dutch or not self.coherent:
            raise DraftRejected("De tekst is taalkundig of inhoudelijk niet goedgekeurd.")
        if self.level_issue.strip():
            raise DraftRejected(self.level_issue)
        rejected = tuple(q for q in self.questions if not q.valid or q.answer_idx != expected[q.key])
        if rejected:
            reason = "\n".join(f"Vraag {q.key} is niet eenduidig goedgekeurd: {q.explanation}" for q in rejected)
            raise DraftRejected(reason, rejected)


class QuestionRepairs(practice.Content):
    """Replace only flagged questions, e.g. keep other independently supported answers verbatim."""

    questions: list[Question]

    def apply(self, passage: Passage, rejected: tuple[AuditQuestion, ...]) -> Passage:
        """Keep each question's number and kind so a repair cannot silently shrink the round."""

        replacements = {q.key: q for q in self.questions}
        wanted = {q.key for q in rejected}
        if replacements.keys() != wanted or len(self.questions) != len(wanted):
            raise ValueError("De reparatie mist afgekeurde vragen of bevat extra vragen.")
        questions = [replacements.get(q.key, q) for q in passage.questions]
        if any(old.kind != new.kind for old, new in zip(passage.questions, questions, strict=True)):
            raise ValueError("De reparatie veranderde de gevraagde vraagsoorten.")
        return passage.model_copy(update={'questions': questions})


class CriterionResult(practice.Content):
    """Award one predefined content point; e.g. good paraphrases receive credit without requiring the model answer verbatim."""

    key: str
    met: bool
    explanation: str


class WordResult(practice.Content):
    """Separate lexical evidence from general comprehension; e.g. an unrelated inference error does not fail every passage word."""

    word_id: int
    correct: bool | None
    explanation: str


class Error(practice.Content):
    """Localize a Dutch correction for hover feedback; e.g. a valid word with the wrong meaning remains distinct from spelling."""

    kind: Literal['meaning', 'grammar', 'spelling', 'word_order', 'omission', 'extra', 'other']
    evidence: str
    correction: str
    explanation: str


class Grade(practice.Content):
    """Correct Dutch writing separately from content understanding; e.g. a correct idea with an article error retains comprehension credit."""

    confident: bool
    criteria: list[CriterionResult]
    language_correct: bool
    corrected: str
    corrected_english: str
    explanation: str
    words: list[WordResult]
    errors: list[Error]
    vocabulary: list[learning.Correction]

    def check(self, answer: str, question: Question) -> None:
        """Ground all grading in the frozen rubric and actual answer; e.g. uncertain grading never advances shared review state."""

        if not self.confident or not self.corrected.strip():
            raise ValueError("De beoordeling is onvoldoende zeker; je antwoord blijft bewaard.")
        if {c.key for c in self.criteria} != {c.key for c in question.rubric} or len(self.criteria) != len(question.rubric):
            raise ValueError("De beoordelaar heeft niet alle criteria afzonderlijk beoordeeld.")
        if {w.word_id for w in self.words} != set(question.word_ids) or len(self.words) != len(question.word_ids):
            raise ValueError("De woordbeoordeling is onvolledig.")
        if any(e.evidence and e.evidence.casefold() not in answer.casefold() for e in self.errors):
            raise ValueError('Een foutmarkering staat niet in het ingediende antwoord.')
        learning.Corrections(vocabulary=self.vocabulary).check(self.learning_document(answer))

    @property
    def score(self) -> float:
        """Compute partial comprehension locally from fixed criteria; e.g. two of three points is 67%, independent from grammar."""

        return sum(c.met for c in self.criteria) / len(self.criteria) if self.criteria else 1.0

    def learning_document(self, answer: str) -> dict:
        """Adapt Dutch feedback to the shared bilingual vocabulary contract; e.g. zekerder can teach zeker and veilig without showing English in the exercise."""

        return dict(origin="comprehension", direction="en_nl", answer=answer, correct=self.score == 1 and self.language_correct,
                    dutch=self.corrected, english=self.corrected_english, errors=[e.model_dump() for e in self.errors], explanation=self.explanation)
