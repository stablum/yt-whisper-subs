"""Tests for local Whisper launch policy and output validation.

Example: `python -m unittest tests.test_whisper_local`.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from yt_whisper_subs import whisper_local
from yt_whisper_subs import srt


class WhisperLaunchTests(unittest.TestCase):
    """Keep anti-loop decoding and atomic subtitle replacement together.

    Example: `WhisperLaunchTests("test_launch_enables_anti_loop_decoding")`.
    """

    @mock.patch("yt_whisper_subs.whisper_local.remove_invalid_whisper_model_cache")
    @mock.patch("yt_whisper_subs.whisper_local.proc.run")
    def test_launch_enables_anti_loop_decoding(self, run: mock.Mock, _cache: mock.Mock) -> None:
        """Use word timestamps and disable previous-text conditioning by default.

        Example: the reported station-ident loop is avoided at transcription time.
        """

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            audio = root / "video.opus"
            output = root / "video.srt"
            audio.write_bytes(b"audio")

            def create_srt(cmd: list[object]) -> None:
                """Materialize the tiny output that a mocked Whisper command promises.

                Example: `proc.run` writes beside its `--output_dir` argument.
                """

                parts = [str(part) for part in cmd]
                result = Path(parts[parts.index("--output_dir") + 1]) / "video.srt"
                result.write_text("1\n00:00:00,000 --> 00:00:01,000\nHallo\n", encoding="utf-8")

            run.side_effect = create_srt
            args = argparse.Namespace(task="transcribe", language="nl", model="turbo", device="cuda")
            whisper_local.run_whisper(audio, output, root, {"whisper": Path("whisper")}, args)
            self.assertTrue(output.is_file())

        cmd = [str(part) for part in run.call_args.args[0]]
        self.assertEqual(cmd[cmd.index("--condition_on_previous_text") + 1], "False")
        self.assertEqual(cmd[cmd.index("--word_timestamps") + 1], "True")
        self.assertEqual(cmd[cmd.index("--hallucination_silence_threshold") + 1], "2")

    @mock.patch("yt_whisper_subs.whisper_local.remove_invalid_whisper_model_cache")
    @mock.patch("yt_whisper_subs.whisper_local.proc.run")
    def test_looped_result_does_not_replace_existing_file(self, run: mock.Mock, _cache: mock.Mock) -> None:
        """Reject a remaining loop before it overwrites the prior durable sidecar.

        Example: a failed retry leaves the old file available for diagnosis.
        """

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            audio = root / "video.opus"
            output = root / "video.srt"
            audio.write_bytes(b"audio")
            output.write_text("old", encoding="utf-8")

            def create_loop(cmd: list[object]) -> None:
                """Write a synthetic three-minute hallucination to the temp output.

                Example: validation rejects this mocked Whisper result.
                """

                parts = [str(part) for part in cmd]
                result = Path(parts[parts.index("--output_dir") + 1]) / "video.srt"
                result.write_text(
                    "\n\n".join(
                        f"{idx + 1}\n00:0{idx}:00,000 --> 00:0{idx + 1}:00,000\nStation ident"
                        for idx in range(3)
                    ),
                    encoding="utf-8",
                )

            run.side_effect = create_loop
            args = argparse.Namespace(task="transcribe", language="nl", model="turbo", device="cuda")
            with self.assertRaisesRegex(RuntimeError, "repeated speech-recognition loop"):
                whisper_local.run_whisper(audio, output, root, {"whisper": Path("whisper")}, args)

            self.assertEqual(output.read_text(encoding="utf-8"), "old")

    @mock.patch("yt_whisper_subs.whisper_local.remove_invalid_whisper_model_cache")
    @mock.patch("yt_whisper_subs.whisper_local.proc.run")
    def test_recovers_gap_before_publishing_whisper_output(self, run: mock.Mock, _cache: mock.Mock) -> None:
        """Exercise the actual launch boundary; e.g. confidently recovered speech reaches the final primary SRT."""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "published.srt"
            output.write_text("Previous yield", encoding="utf-8")

            def generate(command) -> None:
                """Produce two known cues, then the missing speech; e.g. the final file stays untouched until both succeed."""

                self.assertEqual(output.read_text(), "Previous yield")
                directory = Path(command[command.index("--output_dir") + 1])
                if command[command.index("--output_format") + 1] == "srt":
                    text = ("1\n00:08:14,980 --> 00:08:22,120\nEerdere tekst.\n\n"
                            "2\n00:08:44,980 --> 00:08:52,500\nLatere tekst.\n")
                    (directory / "video.srt").write_text(text, encoding="utf-8")
                else:
                    segment = {"start": 517.4, "end": 522.44, "text": "Er kan opvang geregeld worden.",
                               "words": [{"probability": 0.95}] * 5, "avg_logprob": -0.15,
                               "no_speech_prob": 0.01, "compression_ratio": 1.5}
                    (directory / "video.json").write_text(json.dumps({"segments": [segment]}), encoding="utf-8")

            run.side_effect = generate
            args = argparse.Namespace(task="transcribe", language="nl", model="turbo", device="cuda", compact_line_width=50)
            whisper_local.run_whisper(root / "video.opus", output, root, {"whisper": Path("whisper")}, args)
            cues = srt.parse_srt(output.read_text())
            self.assertEqual(len(cues), 3)
            self.assertEqual(cues[1], srt.SubtitleCue(517400, 522440, "Er kan opvang geregeld worden."))
            self.assertEqual(run.call_count, 2)


if __name__ == "__main__":
    unittest.main()
