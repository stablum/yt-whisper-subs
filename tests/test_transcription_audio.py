"""Exercise the local audio evidence gate around real Whisper output structure."""

from __future__ import annotations

import unittest

from yt_whisper_subs import srt
from yt_whisper_subs import transcription_audio


def segment(phrase: str, prob: float = 0.96, *, start: float = 0.4) -> dict:
    """Represent independently timed words, e.g. a local decode of 'eigenlijk amper kunnen'."""

    words = []
    for idx, word in enumerate(phrase.split()):
        at = start + idx * 0.2
        words.append({"word": f" {word}", "start": at, "end": at + 0.15, "probability": prob})
    return {"words": words, "avg_logprob": -0.13, "no_speech_prob": 0.03, "compression_ratio": 1.1}


class TranscriptionAudioTests(unittest.TestCase):
    """Check that acoustic evidence is exact, unique and inside the requested cue."""

    def test_local_decode_requires_exact_confident_anchors(self) -> None:
        """Accept the anchored word and reject nearby, low-confidence or competing readings."""

        cue = srt.SubtitleCue(174380, 178180, "We hebben eigenlijk een hamper kunnen spelen.")
        match = transcription_audio.Match(cue, ("eigenlijk", "amper", "kunnen"))
        good = segment("Eigenlijk amper kunnen.")
        for segments, confirmed in (
            ([good], True),
            ([segment("eigenlijk een handbal kunnen")], False),
            ([segment("eigenlijk een amper kunnen")], False),
            ([segment("wij amper kunnen")], False),
            ([segment("eigenlijk amper kunnen", prob=0.65)], False),
            ([segment("eigenlijk amper kunnen", start=0.1)], False),
            ([{**good, "no_speech_prob": 0.5}], False),
            ([good, segment("eigenlijk amper kunnen", start=2)], False),
            ([good, segment("eigenlijk amper kunnen", prob=0.65, start=2)], False),
        ):
            with self.subTest(segments=segments):
                self.assertEqual(transcription_audio._confirmed({"segments": segments}, match, 174080), confirmed)
