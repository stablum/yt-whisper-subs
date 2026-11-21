"""Present saved practice codes without translating their storage identities.

Example: `term("word_order")` labels an error in the selected interface language.
"""

from yt_whisper_subs import i18n


_TERMS = {
    "active": i18n.mark("In progress"), "completed": i18n.mark("Completed"),
    "stopped": i18n.mark("Stopped"), "ready": i18n.mark("Ready"),
    "correct": i18n.mark("Correct"), "wrong": i18n.mark("To revisit"),
    "invalid": i18n.mark("Reported"), "queued": i18n.mark("Queued"),
    "checking": i18n.mark("Checking"), "failed": i18n.mark("Failed"),
    "tense": i18n.mark("Tense"), "subject": i18n.mark("Subject"),
    "structure": i18n.mark("Structure"), "verb": i18n.mark("Verb"),
    "agreement": i18n.mark("Agreement"), "verb_spelling": i18n.mark("Verb spelling"),
    "auxiliary": i18n.mark("Auxiliary"), "participle": i18n.mark("Participle"),
    "infinitive": i18n.mark("Infinitive"), "word_order": i18n.mark("Word order"),
    "separable_prefix": i18n.mark("Separable prefix"), "reflexive": i18n.mark("Reflexive"),
    "negation": i18n.mark("Negation"), "mood": i18n.mark("Mood"),
    "meaning": i18n.mark("Meaning"), "other": i18n.mark("Other"),
    "omission": i18n.mark("Omission"), "extra_content": i18n.mark("Extra content"),
    "verb_form": i18n.mark("Verb form"), "article": i18n.mark("Article"),
    "preposition": i18n.mark("Preposition"), "spelling": i18n.mark("Spelling"),
    "degree": i18n.mark("Degree"), "number": i18n.mark("Number"),
    "idiom": i18n.mark("Idiom"), "transcription": i18n.mark("Transcription"),
}


def term(key: str) -> str:
    """Translate known codes while preserving unexpected diagnostics; e.g. old raw errors remain readable."""

    return i18n.tr(_TERMS.get(key, key))


def graded_accuracy(correct: int, graded: int) -> str:
    """Label accuracy in activity details, e.g. pending answers contribute to effort only."""

    return i18n.tr(
        '{value:.1%} correct ({correct}/{graded} graded)',
        value=correct / graded, correct=correct, graded=graded,
    ) if graded else i18n.tr('No graded answers')
