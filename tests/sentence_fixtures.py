"""Offline bilingual sentence fixtures with independent word and morphology judgments.

Example: tests can miss a superlative without failing bicycle or conditional-verb recall in the same sentence.
"""

from __future__ import annotations

from tests import verb_fixtures
from yt_whisper_subs import practice
from yt_whisper_subs import sentence_practice as sentences


DUTCH = "De slimste buurman zou eventueel naar de fietsen lopen."
ENGLISH = "The cleverest neighbour would possibly walk to the bicycles."
ENTRIES = [
    {"surface": "slim", "lemma": "slim", "pos": "adjective", "sentence": "Hij is slim.", "meaning": "clever"},
    {"surface": "fiets", "lemma": "fiets", "pos": "noun", "sentence": "Mijn fiets is rood.", "meaning": "bicycle"},
    {"surface": "liep", "lemma": "lopen", "pos": "verb", "sentence": "Ik liep naar huis.", "meaning": "walked"},
]


def seed(db) -> list[int]:
    """Save genuine clicked words and translations; e.g. the supporting neighbour is intentionally absent from vocabulary."""

    return verb_fixtures.seed_vocab(db, ENTRIES)


def options(**changes) -> sentences.Options:
    """Use every fixture word in one natural sentence; e.g. a specific-error test never depends on random companion selection."""

    return sentences.Options(count=1, words=3, scene="shopping", **changes)


class FakeSentenceProvider:
    """Supply controlled semantic and grammar outcomes while preserving the production model contracts."""

    model = "test-sentence-provider"
    reasoning = "low"

    def __init__(self) -> None:
        """Track actual requests independently; e.g. exact approved answers need no grading request."""

        self.calls = []
        self.graded = []
        self.available = True
        self.approved = True
        self.grade_result = None

    def generate(self, plans: list[dict], direction: practice.Direction, focus: list[str]) -> list[dict]:
        """Use varied forms of each requested saved concept; e.g. captured liep appears as the conditional zou lopen."""

        self.calls.append((plans, direction, focus))
        if not self.available:
            raise OSError("Sentence provider unavailable")
        result = []
        forms = {"slim": (["slimste"], ["cleverest"], ["superlative", "adjective_ending"]),
                 "fiets": (["fietsen"], ["bicycles"], ["plural"]),
                 "lopen": (["zou", "lopen"], ["would", "walk"], ["conditional", "person"])}
        for plan in plans:
            uses = []
            for word in plan["words"]:
                dutch, english, features = forms[word["lemma"]]
                uses.append(sentences.WordUse(word_id=word["word_id"], dutch_forms=dutch, english_forms=english,
                                               features=features, explanation="A valid new form of the saved dictionary word."))
            draft = sentences.Draft(anchor_word_id=plan["anchor_word_id"], scene=plan["scene"], dutch=DUTCH, english=ENGLISH,
                                     words=uses, alternatives=[], explanation="Use the superlative, plural and conditional in this situation.")
            verdict = sentences.Verdict(anchor_word_id=plan["anchor_word_id"], valid=self.approved, reason="Controlled independent approval")
            result.append({"draft": draft.model_dump(), "verdict": verdict.model_dump()})
        return result

    def grade(self, document: dict) -> sentences.Grade:
        """Diagnose specific fixture errors or accept synonyms; e.g. an intelligentste paraphrase earns sentence credit without target recall."""

        self.graded.append(document)
        if not self.available:
            raise OSError("Sentence provider unavailable")
        if self.grade_result is not None:
            return self.grade_result
        content, answer = document["exercise"], document["learner_answer"]
        nl = document["direction"] == "en_nl"
        degree = "slimmere" in answer or "cleverer" in answer
        syntax = "eventueel zou" in answer or "possibly would" in answer
        synonym = "intelligentste" in answer or "smartest" in answer
        words, affected = [], []
        for word in content["words"]:
            adjective = "superlative" in word["features"]
            missing_target = synonym and adjective and nl
            fail = degree and adjective
            if fail:
                affected.append(word["word_id"])
            features = []
            for feature in word["features"]:
                value = None if missing_target or (not nl and feature == "adjective_ending") else not (fail and feature == "superlative")
                features.append(sentences.FormResult(feature=feature, correct=value, explanation="Specific controlled form judgment"))
            form_correct = None if missing_target else not fail
            words.append(sentences.WordResult(word_id=word["word_id"], meaning_correct=True, target_used=not missing_target,
                                               form_correct=form_correct, forms=features,
                                               explanation="Use the saved adjective too." if missing_target else "Independent target assessment"))
        errors = []
        if degree:
            evidence, correction = ("slimmere", "slimste") if nl else ("cleverer", "cleverest")
            errors.append(sentences.Error(kind="degree", word_ids=affected, evidence=evidence, correction=correction,
                                          explanation="The source asks for the superlative, not the comparative."))
        if syntax:
            evidence, correction = ("eventueel zou", "zou eventueel") if nl else ("possibly would", "would possibly")
            errors.append(sentences.Error(kind="word_order", word_ids=[], evidence=evidence, correction=correction,
                                          explanation="Correct the finite-verb position while keeping the target meanings."))
        corrected = content["dutch"] if nl else content["english"]
        return sentences.Grade(confident=True, correct=not errors, corrected=corrected if errors else answer,
                                 corrected_meaning=content["english"] if nl else content["dutch"],
                                 explanation="Check the requested degree and sentence structure.", words=words, errors=errors, vocabulary=[])
