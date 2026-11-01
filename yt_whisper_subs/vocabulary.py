"""Describe immutable subtitle occurrences without depending on Qt or a player.

Example: `Transcript.load(path, 'nl').context(0, 2)` identifies one exact word.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import unicodedata
from pathlib import Path
from typing import NamedTuple

from yt_whisper_subs import srt


def normalized(text: str) -> str:
    """Fold case while retaining accents; e.g. `Héél` and `héél` share counts."""

    return unicodedata.normalize("NFC", text).casefold()


class Source(NamedTuple):
    """Snapshot a durable media reference; e.g. history survives file removal."""

    key: str
    video_id: str | None
    title: str
    url: str
    path: str

    @classmethod
    def local(cls, path: Path) -> Source:
        """Identify arbitrary local media by absolute path; e.g. a CLI input file."""

        path = path.resolve()
        digest = hashlib.sha256(os.path.normcase(str(path)).encode()).hexdigest()
        return cls(f"file:{digest}", None, path.stem, path.as_uri(), str(path))


class Token(NamedTuple):
    """Keep rendered punctuation separate from the word span; e.g. `woord,`."""

    display: str
    start: int
    end: int


class Cue(NamedTuple):
    """Keep timing and token offsets on the same source text; e.g. a Dutch cue."""

    start_ms: int
    end_ms: int
    text: str
    tokens: list[Token]


class Context(NamedTuple):
    """Supply the exact occurrence and nearby language; e.g. disambiguate `bank`."""

    language: str
    cue: Cue
    token: Token
    before: str
    after: str
    english: str

    @property
    def surface(self) -> str:
        """Read the selected source span; e.g. retain the case of `Nederlandse`."""

        return self.cue.text[self.token.start:self.token.end]

    def document(self) -> dict[str, object]:
        """Serialize only linguistic context; e.g. exclude the video's private path."""

        return {
            "language": self.language, "target_language": "en", "word": self.surface,
            "sentence": self.cue.text, "span": [self.token.start, self.token.end],
            "before": self.before, "after": self.after, "english": self.english,
        }


class Selection(NamedTuple):
    """Separate capture time from cue time; e.g. click during an extended cue gap."""

    source: Source
    context: Context
    position: float
    clicked_at: float
    event_id: str


class LookupIdentity(NamedTuple):
    """Persist request policy beside its cache key; e.g. retries retain the original model."""

    key: str
    model: str
    version: int
    reasoning: str


class Meaning(NamedTuple):
    """Describe the meaning at one occurrence; e.g. `liep` means `walked` here."""

    meaning: str
    lemma: str
    lemma_meaning: str
    part_of_speech: str
    note: str
    expression: str
    ambiguous: bool

    @classmethod
    def parse(cls, text: str) -> Meaning:
        """Reject malformed or empty model results; e.g. never persist partial JSON."""

        data = json.loads(text)
        if not isinstance(data, dict) or set(data) != set(cls._fields):
            raise ValueError("word translation has unexpected fields")
        for field in cls._fields[:-1]:
            if not isinstance(data[field], str) or len(data[field]) > 2000:
                raise ValueError(f"invalid word translation field: {field}")
            data[field] = data[field].strip()
        if type(data["ambiguous"]) is not bool or not data["meaning"] or not data["lemma"]:
            raise ValueError("word translation needs a meaning, lemma, and ambiguity flag")
        return cls(**data)


class Transcript:
    """Own source text and tokenization once for Python and Lua; e.g. avoid drift."""

    def __init__(self, language: str, cues: list[Cue]) -> None:
        """Keep cue indices stable for a launch; e.g. repeated words use distinct spans."""

        self.language = language
        self.cues = cues

    @classmethod
    def load(cls, path: Path, language: str) -> Transcript:
        """Tokenize Unicode words while preserving display text; e.g. `geïnteresseerd`."""

        cues = []
        for cue in srt.parse_srt(path.read_text(encoding="utf-8-sig")):
            text = html.unescape(re.sub(r"<[^>]*>", "", cue.text))
            tokens = []
            for part in re.finditer(r"\S+", text):
                matches = list(re.finditer(r"\w+(?:['’\-]\w+)*", part.group()))
                start = part.start() + matches[0].start() if matches else part.start()
                end = part.start() + matches[-1].end() if matches else start
                tokens.append(Token(part.group(), start, end))
            cues.append(Cue(cue.start_ms, cue.end_ms, text, tokens))
        return cls(language, cues)

    def context(self, cue_idx: int, token_idx: int, english: Transcript | None = None) -> Context:
        """Bind a token to its source sentence and two neighbors; e.g. split verbs."""

        cue = self.cues[cue_idx]
        token = cue.tokens[token_idx]
        if token.end <= token.start:
            raise ValueError("select a word rather than punctuation")
        before = " ".join(item.text for item in self.cues[max(0, cue_idx - 2):cue_idx])
        after = " ".join(item.text for item in self.cues[cue_idx + 1:cue_idx + 3])
        translated = ""
        if english and english is not self:
            overlaps = [item.text for item in english.cues
                        if item.start_ms < cue.end_ms and item.end_ms > cue.start_ms]
            translated = " ".join(overlaps)
        return Context(self.language, cue, token, before, after, translated)

    def payload(self) -> list[dict[str, object]]:
        """Give Lua authoritative indices and word spans; e.g. no duplicate tokenizer."""

        return [{"start": cue.start_ms / 1000, "end": cue.end_ms / 1000,
                 "text": cue.text, "tokens": [dict(token._asdict(), line=cue.text.count("\n", 0, token.start))
                                              for token in cue.tokens]}
                for cue in self.cues]
