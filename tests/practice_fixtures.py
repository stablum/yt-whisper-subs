"""Reusable isolated vocabulary and deterministic question strategies for practice tests.

Example: `seed(db)` creates real relational Dutch vocabulary without network calls.
"""

from __future__ import annotations

import time

from yt_whisper_subs import practice
from yt_whisper_subs import vocabulary
from yt_whisper_subs import vocabulary_translate


TERMS = [("liep", "walked", "Ik liep naar huis."), ("eventueel", "possibly", "Ik kom eventueel morgen."),
         ("winkel", "shop", "De winkel is open."), ("slim", "clever", "Dat is een slim plan.")]


def seed(db, terms=TERMS) -> None:
    """Capture and resolve genuine vocabulary rows; e.g. preserve the same source spans used by mpv."""

    translator = vocabulary_translate.WordTranslator()
    for idx, (surface, meaning, sentence) in enumerate(terms):
        start = sentence.index(surface)
        token = vocabulary.Token(surface, start, start + len(surface))
        cue = vocabulary.Cue(1000, 5000, sentence, [token])
        ctx = vocabulary.Context("nl", cue, token, "", "", "")
        source = vocabulary.Source("video", "video", "Dutch practice", "https://youtu.be/video", "video.mkv")
        selection = vocabulary.Selection(source, ctx, 2, time.time() + idx, f"word:{idx}")
        row = db.save_word(selection, translator.cache_identity(ctx))
        db.claim_word_lookup("fixture")
        result = vocabulary.Meaning(meaning, surface, meaning, "term", "", "", False)
        db.finish_word_lookup(row["id"], "fixture", result)


class FakeGenerator:
    """Supply realistic display content with controllable failure and rejection paths."""

    model, reasoning = "test-provider", "low"

    def __init__(self) -> None:
        """Track actual batch calls; e.g. ensure cached rounds still work offline."""

        self.calls = []
        self.available = True
        self.rejected = set()

    def generate(self, rows: list[dict], direction: practice.Direction) -> list[dict]:
        """Keep fixed correct answers in the catalog; e.g. reverse practice gets only incorrect Dutch options."""

        self.calls.append((rows, direction))
        if not self.available:
            raise ConnectionError("test provider offline")
        en = {"liep": ("ran", "stood"), "eventueel": ("eventually", "currently"),
              "winkel": ("market", "office"), "slim": ("slender", "cautious")}
        nl = {"liep": ("rende", "stond"), "eventueel": ("uiteindelijk", "momenteel"),
              "winkel": ("markt", "kantoor"), "slim": ("slank", "voorzichtig")}
        results = []
        for row in rows:
            choices = (en if direction == practice.Direction.NL_EN else nl)[row["surface"]]
            distractors = [{"text": text, "explanation": f"{text} has a different meaning in this context.",
                            "kind": "near_meaning"} for text in choices]
            results.append({"draft": {"lookup_id": row["lookup_id"], "distractors": distractors,
                                      "explanation": f"Here, {row['surface']} means {row['meaning']}."},
                            "verdict": {"lookup_id": row["lookup_id"], "valid": row["lookup_id"] not in self.rejected,
                                        "reason": "test quality verdict"}})
        return results
