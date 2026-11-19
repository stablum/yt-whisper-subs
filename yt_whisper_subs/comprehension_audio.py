"""Narrate long passages in reusable bounded segments with existing voice presets.

Example: a five-paragraph article never becomes one oversized Zonos generation.
"""

import re
import textwrap
import time

from yt_whisper_subs import listening_tts
from yt_whisper_subs import listening_voices


def segments(paragraphs: list[str]) -> list[dict]:
    """Preserve every word while bounding synthesis work; e.g. long sentences split only at whitespace, never inside a word."""

    result = []
    for paragraph, text in enumerate(paragraphs, 1):
        for sentence in re.split(r'(?<=[.!?])\s+', text.strip()):
            for part in textwrap.wrap(sentence, width=150, break_long_words=False, break_on_hyphens=False):
                result.append(dict(index=len(result), paragraph=paragraph, text=part))
    return result


class Audio:
    """Prepare only the chosen narrator for a long exercise; e.g. six configured voices do not multiply a whole article's CPU preparation."""

    def __init__(self, db, speech=None) -> None:
        """Allow deterministic offline speech; e.g. tests verify transcript concealment without loading a model."""

        self._db, self._speech = db, speech

    def _engine(self, preset):
        """Use the same local speech strategies as dictation; e.g. Piper and the user's Zonos experiments remain available."""

        preset = preset or listening_voices.read(self._db).selected
        return self._speech or listening_voices.speech(self._db, preset.config)

    def prepare(self, passage_id: int, report, *, preset=None) -> None:
        """Finish all normal and slow segments before publishing readiness; e.g. a failed last paragraph leaves approved text reusable."""

        passage = self._db.comprehension_passage(passage_id)['content']
        parts = segments(passage['paragraphs'])
        assets = listening_tts.Recordings(self._db).prepare(self._engine(preset), [p['text'] for p in parts], report)
        with self._db._connect() as conn:
            conn.executemany('INSERT OR IGNORE INTO comprehension_audio VALUES(?,?,?)',
                             [(passage_id, part['index'], assets[part['text']]) for part in parts])

    def rows(self, passage_id: int, *, preset=None) -> list[dict]:
        """Return playback metadata without requiring a visible transcript; e.g. progress labels show paragraph numbers only."""

        passage = self._db.comprehension_passage(passage_id)['content']
        profile, result = self._engine(preset).profile, []
        for part in segments(passage['paragraphs']):
            key = listening_tts.Recordings.key(profile, part['text'])
            asset = self._db.audio_asset(key)
            if asset is None or not self._db.listening_audio_valid(asset):
                raise ValueError('De spraak is nog niet volledig voorbereid.')
            result.append(dict(index=part['index'], paragraph=part['paragraph'], paths=self._db.listening_paths(key), **asset))
        return result

    def ready(self, passage_id: int) -> bool:
        """Check both speeds without synthesis; e.g. prefetch repairs a deleted segment rather than generating another article."""

        try:
            return bool(self.rows(passage_id))
        except (OSError, ValueError):
            return False

    @staticmethod
    def valid(row: dict, speed: str) -> bool:
        """Verify the actual selected recording before playback; e.g. clearing an audio cache midway through a passage is recoverable."""

        try:
            return listening_tts.digest(row['paths'][speed]) == row[speed + '_sha256']
        except OSError:
            return False

    def played(self, session_id: str, row: dict, speed: str) -> None:
        """Track actual listening separately from answers; e.g. replaying an article adds no daily-practice credit."""

        with self._db._connect() as conn:
            conn.execute('INSERT INTO comprehension_plays(session_id,cache_key,segment,speed,played_at) VALUES(?,?,?,?,?)',
                         (session_id, row['cache_key'], row['index'], speed, time.time()))
