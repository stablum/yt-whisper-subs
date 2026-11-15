"""Grade dictation against the actual spoken script without a slow remote judge.

Example: a synonym is a translation alternative, but is not the word pronounced in a dictation.
"""

import difflib
import re

from yt_whisper_subs import sentence_practice as sentences
from yt_whisper_subs import verb_practice


VERSION = 1


def _tokens(text: str) -> list[tuple[str, int, int]]:
    """Retain original character evidence beside normalized words; e.g. punctuation and case do not cause dictation failures."""

    return [(value, match.start(), match.end()) for match in re.finditer(r"\S+", text)
            if (value := verb_practice.normalized(match.group()))]


def grade(answer: str, content: dict) -> sentences.Grade:
    """Give exact word-level credit and localized mismatches; e.g. one missed supporting word does not fail every saved target."""

    expected, typed = _tokens(content["dutch"]), _tokens(answer)
    x, y = [row[0] for row in expected], [row[0] for row in typed]
    matcher = difflib.SequenceMatcher(a=x, b=y, autojunk=False)
    matched = {idx for a, _, size in matcher.get_matching_blocks() for idx in range(a, a + size)}
    targets, words = {}, []
    for word in content["words"]:
        indices, found = set(), True
        for form in word["dutch_forms"]:
            # WordUse evidence may be a substring of an inflected token; align the containing spoken tokens.
            occurrences = re.finditer(re.escape(form), content["dutch"], re.I)
            spans = [{idx for idx, (_, start, end) in enumerate(expected) if start < match.end() and end > match.start()}
                     for match in occurrences]
            found &= any(span <= matched for span in spans)
            indices.update(set.union(*spans) if spans else set())
        targets[word["word_id"]] = indices
        explanation = "Spoken target transcribed correctly." if found else "Listen again to this target's spoken form."
        words.append(sentences.WordResult(word_id=word["word_id"], meaning_correct=found, target_used=found,
                                         form_correct=True if found else None, explanation=explanation,
                                         forms=[sentences.FormResult(feature=feat, correct=True if found else None,
                                                                     explanation=explanation) for feat in word["features"]]))
    failed = {word.word_id for word in words if not word.correct}
    errors = []
    for tag, a, b, c, d in matcher.get_opcodes():
        if tag == "equal":
            continue
        source = content["dutch"][expected[a][1]:expected[b - 1][2]] if a < b else ""
        evidence = answer[typed[c][1]:typed[d - 1][2]] if c < d else ""
        affected = [key for key in failed if targets[key] & set(range(a, b))]
        errors.append(sentences.Error(kind={"delete": "omission", "insert": "extra_content", "replace": "transcription"}[tag],
                                      word_ids=affected, evidence=evidence, correction=source or "Remove this extra text.",
                                      source_forms=[source] if source else [],
                                      explanation="Write the words that were spoken; capitalization and sentence punctuation are ignored."))
    covered = {key for error in errors for key in error.word_ids}
    if failed - covered or len(errors) > 15:
        errors = [sentences.Error(kind="other", word_ids=sorted(failed), evidence=answer,
                                  correction=content["dutch"], source_forms=[content["dutch"]],
                                  explanation="Compare your transcription with the spoken sentence, including every saved target.")]
    result = sentences.Grade(confident=True, correct=not errors, corrected=answer if not errors else content["dutch"],
                             corrected_meaning=content["english"], words=words, errors=errors, vocabulary=[],
                             explanation="The Dutch text is the sentence you heard. The English below gives its meaning. "
                                         "Word credit here measures transcription, rather than a separate meaning test.")
    result.check(answer, content, "listen_nl")
    return result
