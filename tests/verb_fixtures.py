"""Small Dutch teaching corpus and an isolated diagnostic provider.

Example: work/make transformations exercise persistence without calling an LLM.
"""

from yt_whisper_subs import verb_practice as verbs


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

    def generate(self, targets: list[dict]) -> list[dict]:
        """Use a fixed present-jij corpus; e.g. tests restrict the local catalog to werken and maken."""

        self.generated += 1
        if not self.available:
            raise OSError("Provider unavailable")
        result = []
        for target in targets:
            make = target["lemma"] == "maken"
            answer = "Jij maakt vandaag soep." if make else "Jij werkt vandaag."
            draft = verbs.Draft(target_id=target["target_id"],
                                source="Ik maakte gisteren soep." if make else "Ik werkte gisteren.",
                                instruction="Change to jij in the present tense; replace gisteren with vandaag.",
                                answer=answer, alternatives=[answer.replace("Jij", "Je")],
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
                           explanation="Jij needs the present ending -t in this statement.",
                           axes=[verbs.Axis(axis=axis, correct=axis != "subject") for axis in verbs.AXES],
                           errors=[verbs.Error(kind="agreement", axis="subject", evidence=stem,
                                               correction=form, explanation="Add -t for second-person singular.")])


def options(count: int = 2) -> verbs.Options:
    """Limit the corpus to factual deterministic transformations; e.g. test scheduling independently of language generation."""

    return verbs.Options(count=count, focus="present", subject="jij", structure="statement")


def catalog(db) -> list[dict]:
    """Select two real relational lemmas; e.g. IDs remain governed by the production database."""

    return [row for row in db.verb_catalog() if row["lemma"] in {"werken", "maken"}]
