"""Small Dutch teaching corpus and an isolated diagnostic provider.

Example: work/make transformations exercise persistence without calling an LLM.
"""

from yt_whisper_subs import verb_practice as verbs
from yt_whisper_subs import vocabulary
from yt_whisper_subs import vocabulary_translate

import time


class FakeVerbProvider:
    """Generate known sentences and injectable corrections; e.g. simulate an ambiguous judgment or network failure."""

    model = "test-verb-provider"
    reasoning = "low"

    def __init__(self) -> None:
        """Count requests separately; e.g. approved exact answers must bypass remote grading."""

        self.generated = 0
        self.graded = 0
        self.available = True
        self.approved = True
        self.grade_result = None
        self.lexical_calls = []
        self.translated = []
        self.targets = []

    def generate(self, targets: list[dict]) -> list[dict]:
        """Use a fixed present-jij corpus; e.g. tests restrict the local catalog to werken and maken."""

        self.generated += 1
        self.targets.extend(targets)
        if not self.available:
            raise OSError("Provider unavailable")
        result = []
        for target in targets:
            make = target["lemma"] == "maken"
            answer = "Jij maakt vandaag soep." if make else "Jij werkt vandaag."
            draft = verbs.Draft(target_id=target["target_id"],
                                source="Ik maakte gisteren soep." if make else "Ik werkte gisteren.",
                                source_english="I made soup yesterday." if make else "I worked yesterday.",
                                instruction="Change to jij in the present tense; replace gisteren with vandaag.",
                                answer=answer, answer_english="You make soup today." if make else "You work today.",
                                alternatives=[answer.replace("Jij", "Je")],
                                explanation="Use the present stem plus -t for jij in a statement.")
            verdict = verbs.Verdict(target_id=target["target_id"], valid=self.approved, reason="Known teaching example")
            result.append({"draft": draft.model_dump(), "verdict": verdict.model_dump()})
        return result

    def grade(self, document: dict) -> verbs.Grade:
        """Return an injected assessment or diagnose missing -t; e.g. an error is attributed only to agreement."""

        self.graded += 1
        if not self.available:
            raise OSError("Provider unavailable")
        if self.grade_result is not None:
            return self.grade_result
        make = document["target"]["lemma"] == "maken"
        stem, form = ("maak", "maakt") if make else ("werk", "werkt")
        return verbs.Grade(confident=True, correct=False, corrected=document["exercise"]["answer"],
                           corrected_english=document["exercise"]["answer_english"],
                           explanation="Jij needs the present ending -t in this statement.",
                           axes=[verbs.Axis(axis=axis, correct=axis != "subject") for axis in verbs.AXES],
                           errors=[verbs.Error(kind="agreement", axis="subject", evidence=stem,
                                               correction=form, explanation="Add -t for second-person singular.")])

    def lexemes(self, rows: list[dict]) -> list[dict]:
        """Supply auditable lexical facts for a small test corpus; e.g. two inflected occurrences deduplicate to fietsen."""

        self.lexical_calls.append(rows)
        if not self.available:
            raise OSError("Provider unavailable")
        result = []
        for row in rows:
            lemma = row["lemma"].lower()
            family = "regular"
            if lemma in {"opbelde", "bellen"}:
                lemma, family = "opbellen", "separable"
            if lemma == "vergissen":
                lemma, family = "zich vergissen", "reflexive"
            accepted = lemma != "huis"
            lexeme = verbs.Lexeme(lookup_id=row["lookup_id"], is_verb=accepted, lemma=lemma if accepted else "",
                                  family=family if accepted else None, transitive=(lemma == "opbellen") if accepted else None,
                                  reason="Known contextual test mapping")
            verdict = verbs.LexemeCheck(lookup_id=row["lookup_id"], valid=True, reason="Checked test mapping")
            result.append({"lexeme": lexeme.model_dump(), "verdict": verdict.model_dump()})
        return result

    def translate_sentence(self, sentence: str) -> str:
        """Record exactly what language help receives; e.g. tests prove that source help never sends the answer."""

        self.translated.append(sentence)
        if not self.available:
            raise OSError("Provider unavailable")
        return {"Ik maakte gisteren soep.": "I made soup yesterday.", "Jij werkt vandaag.": "You work today.",
                "Jij maakt vandaag soep.": "You make soup today."}.get(sentence, "I worked yesterday.")


def options(count: int = 2) -> verbs.Options:
    """Limit the corpus to factual deterministic transformations; e.g. test scheduling independently of language generation."""

    return verbs.Options(count=count, focus="present", subject="jij", structure="statement")


def catalog(db) -> list[dict]:
    """Select two real relational lemmas; e.g. IDs remain governed by the production database."""

    return [row for row in db.verb_catalog() if row["lemma"] in {"werken", "maken"}]


def seed_vocab(db, entries: list[dict]) -> list[int]:
    """Capture and translate genuine source records; e.g. saved fietste and gefietst share one dictionary verb."""

    translator = vocabulary_translate.WordTranslator()
    ids = []
    for idx, entry in enumerate(entries):
        surface, lemma = entry["surface"], entry["lemma"]
        sentence = entry.get("sentence", f"Hij {surface} vandaag.")
        start = sentence.index(surface)
        token = vocabulary.Token(surface, start, start + len(surface))
        cue = vocabulary.Cue(1000, 5000, sentence, [token])
        context = vocabulary.Context(entry.get("language", "nl"), cue, token, "", "", "")
        key = entry.get("video", f"video:{idx}")
        source = vocabulary.Source(key, key, "Vocabulary verb source", f"https://youtu.be/{key}", f"{key}.mkv")
        selection = vocabulary.Selection(source, context, 2, time.time(), entry.get("event", f"capture:{idx}"))
        saved = db.save_word(selection, translator.cache_identity(context))
        claimed = db.claim_word_lookup("verb-fixture")
        if claimed:
            meaning = vocabulary.Meaning(entry.get("meaning", "meaning"), lemma, entry.get("meaning", "meaning"),
                                         entry.get("pos", "verb"), "", entry.get("expression", ""), entry.get("ambiguous", False))
            db.finish_word_lookup(saved["id"], "verb-fixture", meaning)
        ids.append(saved["id"])
    return ids
