"""Tests for local Whisper launch policy and output validation.

Example: `python -m unittest tests.test_whisper_local`.
"""

from __future__ import annotations

import argparse
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from yt_whisper_subs import whisper_local


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


if __name__ == "__main__":
    unittest.main()
