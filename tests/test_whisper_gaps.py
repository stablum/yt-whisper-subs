"""Exercise bounded audio retries without CUDA or fabricated gap text.

Example: `python -m unittest tests.test_whisper_gaps`.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from yt_whisper_subs import srt
from yt_whisper_subs import whisper_gaps as gaps


def segment(start: float = 505, end: float = 508, **changes) -> dict:
    """Build confident decoder evidence; e.g. the actual 8:40 omission had high-probability speech."""

    return {"start": start, "end": end, "text": "Er kan opvang geregeld worden.",
            "words": [{"probability": 0.95}] * 5, "avg_logprob": -0.15,
            "no_speech_prob": 0.01, "compression_ratio": 1.5, **changes}


class GapRecoveryTests(unittest.TestCase):
    """Protect existing cue ownership and filter speculative speech; e.g. music does not need subtitles."""

    def test_find_limits_ranges_and_respects_overlapping_existing_cues(self) -> None:
        """Retry only holes in the union of real cue coverage; e.g. a nested short cue creates no false gap."""

        cues = [srt.SubtitleCue(0, 20000, "Long"), srt.SubtitleCue(5000, 6000, "Nested"),
                srt.SubtitleCue(20000, 22000, "Next"), srt.SubtitleCue(40000, 41000, "After gap"),
                srt.SubtitleCue(80000, 81000, "Long silence"), srt.SubtitleCue(84000, 85000, "Brief gap")]
        self.assertEqual(gaps.find(cues), [(22000, 40000)])
        many = [srt.SubtitleCue(i * 11000, i * 11000 + 1000, "Speech") for i in range(12)]
        self.assertEqual(len(gaps.find(many)), 8)

    def test_accepts_confident_speech_at_absolute_source_timestamps(self) -> None:
        """Do not shift local clip offsets into the wrong part of the video; e.g. retain original 505 seconds."""

        result = gaps.recovered_cues({"segments": [segment()]}, [(502120, 524980)])
        self.assertEqual(result, [srt.SubtitleCue(505000, 508000, "Er kan opvang geregeld worden.")])

    def test_rejects_noise_weak_words_outside_ranges_and_repetition(self) -> None:
        """Fail closed on speculative decodes; e.g. silence with a high no-speech probability."""

        variants = [segment(no_speech_prob=0.9), segment(avg_logprob=-1.0),
                    segment(compression_ratio=3.0), segment(words=[{"probability": 0.5}]),
                    segment(start=500), segment(end=530), segment(start=float("nan")),
                    segment(avg_logprob=float("nan")), segment(text="Dank je."), segment(words=[])]
        for candidate in variants:
            with self.subTest(candidate=candidate):
                self.assertEqual(gaps.recovered_cues({"segments": [candidate]}, [(502120, 524980)]), [])
        repeat = [segment(start=505 + i * 3, end=507 + i * 3) for i in range(3)]
        self.assertEqual(gaps.recovered_cues({"segments": repeat}, [(502120, 524980)]), [])

    @mock.patch("yt_whisper_subs.whisper_gaps.proc.run")
    def test_retry_reuses_model_policy_and_changes_only_uncovered_regions(self, run) -> None:
        """Keep all known text/timing while recovering a hole; e.g. preserve both 8:22 and 8:45 boundaries."""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "video.srt"
            original = [srt.SubtitleCue(494980, 502120, "Eerdere gesproken tekst."),
                        srt.SubtitleCue(524980, 532940, "Latere gesproken tekst.")]
            args = argparse.Namespace(compact_line_width=50)
            path.write_text(srt.render_srt(original, args), encoding="utf-8")
            command = ["whisper", "video.opus", "--output_format", "srt", "--output_dir", str(root),
                       "--model", "turbo", "--hallucination_silence_threshold", "2"]

            def decode(retry) -> None:
                """Materialize the retry's JSON; e.g. all offsets are absolute because clip_timestamps is used."""

                directory = Path(retry[retry.index("--output_dir") + 1])
                (directory / "video.json").write_text(json.dumps({"segments": [segment()]}), encoding="utf-8")

            run.side_effect = decode
            self.assertTrue(gaps.recover(path, command, args))
            retry = run.call_args.args[0]
            self.assertEqual(retry[retry.index("--clip_timestamps") + 1], "502.120,524.980")
            self.assertEqual(retry[retry.index("--model") + 1], "turbo")
            self.assertEqual(retry[retry.index("--hallucination_silence_threshold") + 1], "2")
            self.assertEqual(srt.parse_srt(path.read_text()), [original[0], srt.SubtitleCue(505000, 508000,
                             "Er kan opvang geregeld worden."), original[1]])
            self.assertEqual(command[command.index("--output_format") + 1], "srt")

    @mock.patch("yt_whisper_subs.whisper_gaps.proc.run")
    def test_normal_pauses_skip_retry_entirely(self, run) -> None:
        """Avoid routine duplicate work; e.g. a two-second pause needs no second Whisper launch."""

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "video.srt"
            content = "1\n00:00:00,000 --> 00:00:02,000\nHallo.\n\n2\n00:00:04,000 --> 00:00:05,000\nJa.\n"
            path.write_text(content, encoding="utf-8")
            self.assertFalse(gaps.recover(path, [], argparse.Namespace()))
            self.assertEqual(path.read_text(), content)
            run.assert_not_called()
