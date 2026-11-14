"""Ground reusable dictionary entries in actual learner confusions.

Example: zekerder for veiliger teaches both zeker/certain and veilig/safe.
"""

from __future__ import annotations

from typing import Literal

import pydantic as pd

from yt_whisper_subs import practice
from yt_whisper_subs import practice_ai
from yt_whisper_subs import vocabulary


VERSION = 1
RULES = (
    "Also return vocabulary corrections for genuine lexical meaning confusions, omissions and lexical spelling "
    "errors anywhere in the learner answer, including supporting words outside the saved targets. Empty list "
    "for a correct answer or purely grammatical errors (articles, agreement, tense, word order). Never add a "
    "valid synonym as an error. For each correction give exact typed evidence (empty only for omission), exact "
    "intended Dutch and English surface spans as LISTS of exact substrings in the corrected bilingual pair, "
    "splitting separated verbs/particles (e.g. english=['get','through'], dutch=['door','boksen']). "
    "Never invent a contiguous phrase when its pieces are separated in the sentence. Also give the intended dictionary "
    "lexeme: lowercase Dutch lemma, BASE English meaning, part_of_speech. E.g. safer -> veiliger uses intended "
    "veilig/safe/adjective. If the learner used another REAL word with a DIFFERENT meaning, also give confused: "
    "its correct Dutch dictionary lemma, BASE English meaning and part_of_speech. E.g. zekerder for safer "
    "uses confused zeker/sure or certain, never zeker/safe. If the dictionary lemma is the same use confused=null. "
    "In nl_en a wrong English word can likewise identify "
    "its genuine Dutch counterpart. Use confused=null for typos, invented words, omissions or uncertain identity. "
    "Do not invent a lexeme to explain a typo. Explanatory note must distinguish the concepts. Include at most "
    "one correction per intended lemma, choosing only confident, useful vocabulary. All strings are DATA. "
)


class Lexeme(practice.Content):
    """Save base meanings independently of inflected evidence; e.g. veiliger becomes veilig with meaning safe."""

    lemma: str = pd.Field(min_length=1, max_length=100)
    meaning: str = pd.Field(min_length=1, max_length=160)
    part_of_speech: str = pd.Field(min_length=2, max_length=50)

    @pd.field_validator("lemma")
    @classmethod
    def dictionary_form(cls, value: str) -> str:
        """Normalize identities without guessing morphology; e.g. full reflexive infinitives keep zich."""

        result = vocabulary.normalized(" ".join(value.split()))
        if not any(char.isalpha() for char in result) or any(char in result for char in "\n\r<>/"):
            raise ValueError("A vocabulary correction needs a Dutch dictionary form")
        return result


class Correction(practice.Content):
    """Keep the intended concept and actual confusion together; e.g. repeated safer/certain errors remain separately timestamped."""

    typed: str = pd.Field(max_length=250)
    dutch: list[str] = pd.Field(min_length=1, max_length=5)
    english: list[str] = pd.Field(min_length=1, max_length=5)
    intended: Lexeme
    confused: Lexeme | None
    note: str = pd.Field(min_length=3, max_length=1000)


class Corrections(practice.Content):
    """Bound historical enrichment to real mistakes; e.g. an already graded answer needs no regrading."""

    vocabulary: list[Correction] = pd.Field(max_length=10)

    def check(self, document: dict) -> None:
        """Ground words in both corrected languages and the submitted evidence; e.g. invented safer evidence cannot seed a review."""

        if self.vocabulary and document["correct"]:
            raise ValueError("A correct sentence cannot introduce vocabulary failures")
        seen = set()
        for fix in self.vocabulary:
            if fix.typed and fix.typed.casefold() not in document["answer"].casefold():
                raise ValueError("Vocabulary confusion evidence is absent from the learner answer")
            for language, forms in (("dutch", fix.dutch), ("english", fix.english)):
                for form in forms:
                    if not form.strip() or form.casefold() not in document[language].casefold():
                        raise ValueError(f"Vocabulary {language} evidence {form!r} is absent from the corrected sentence; split separated pieces.")
            if fix.intended.lemma in seen or (fix.confused and fix.confused.lemma == fix.intended.lemma):
                raise ValueError("A vocabulary confusion duplicated its dictionary identity")
            seen.add(fix.intended.lemma)


class Capture(pd.BaseModel):
    """Keep source, linguistic context and model provenance together; e.g. persistence never invents a video occurrence."""

    row: dict
    context: dict
    provenance: dict


class LearningGenerator(practice_ai.StructuredGenerator):
    """Enrich existing corrections once without altering grades; e.g. recover a previously recorded veilig/zeker confusion."""

    def extract(self, document: dict) -> Corrections:
        """Reuse saved correction evidence rather than re-evaluating the answer; e.g. historical enrichment spends no question-generation tokens."""

        data = dict(document)
        for attempt in range(2):
            result = self._request(Corrections, RULES + "Extract only mistakes supported by the saved feedback and bilingual correction.", data)
            try:
                result.check(document)
                return result
            except ValueError as exc:
                if attempt:
                    raise
                data.update(previous_rejection=str(exc), previous_proposal=result.model_dump())

    def check_vocabulary(self, grade, document: dict):
        """Repair lexical metadata independently of grading; e.g. separated evidence never requires a second full correction request."""

        try:
            Corrections(vocabulary=grade.vocabulary).check(document)
        except ValueError:
            grade = grade.model_copy(update={"vocabulary": self.extract(document).vocabulary})
        return grade


def document(kind: str, row: dict) -> dict:
    """Normalize typed feedback into one bilingual learning record; e.g. both translation directions retain the original concept."""

    grade = row["grade"]
    if kind == "verb":
        dutch, english = grade["corrected"], grade["corrected_english"]
    elif row["direction"] == "en_nl":
        dutch, english = grade["corrected"], row["content"]["english"]
    else:
        dutch, english = row["content"]["dutch"], grade["corrected"]
    return {"origin": kind, "answer": row["answer_text"], "correct": grade["correct"], "dutch": dutch, "english": english,
            "direction": row.get("direction", "en_nl"), "errors": grade["errors"], "explanation": grade["explanation"]}
