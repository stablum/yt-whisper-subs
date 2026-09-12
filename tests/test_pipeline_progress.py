"""Tests for the opt-in structured progress protocol and phase math.

Example: `python -m unittest tests.test_pipeline_progress`.
"""

from __future__ import annotations

import io
import os
import unittest
from unittest import mock

from yt_whisper_subs import pipeline_progress as progress


class PipelineProgressTests(unittest.TestCase):
    """Keep subprocess events parseable, scoped, and numerically honest.

    Example: `PipelineProgressTests("test_cli_emit_is_opt_in")`.
    """

    def test_protocol_round_trip_and_overall_phase_math(self) -> None:
        """Round-trip Unicode labels and map local work into weighted segments.

        Example: completed downloading reaches the extraction boundary.
        """

        update = progress.make("abc", progress.Stage.DOWNLOADING, 0.5, "Downloading · 50%")
        self.assertEqual(progress.parse(progress.encode(update)), update)
        self.assertAlmostEqual(progress.overall_fraction(update), 0.03 + 0.32 * 0.5)
        self.assertEqual(progress.overall_fraction(progress.make("abc", progress.Stage.READY)), 1.0)
        self.assertAlmostEqual(sum(spec.weight for spec in progress.STAGE_SPECS), 1.0)
        self.assertEqual(progress.stage_label(progress.Stage.CHAPTERING), "Creating chapters")
        cancelled = progress.make("abc", progress.Stage.CANCELLED, 0.42)
        self.assertEqual(progress.stage_label(cancelled.stage), "Cancelled")
        self.assertEqual(progress.overall_fraction(cancelled), 0.42)

    def test_tool_output_advances_only_its_current_stage(self) -> None:
        """Interpret real tool percentages without guessing across phase boundaries.

        Example: Whisper's tqdm line advances speech-to-text, not download.
        """

        downloading = progress.make("abc", progress.Stage.DOWNLOADING)
        update = progress.derive_tool_update("[download]  27.4% of 50MiB", downloading)
        self.assertIsNotNone(update)
        self.assertAlmostEqual(update.fraction, 0.274)
        transcribing = progress.make("abc", progress.Stage.TRANSCRIBING)
        update = progress.derive_tool_update(" 42%|████ | 420/1000 [00:03]", transcribing)
        self.assertIsNotNone(update)
        self.assertEqual(update.fraction, 0.42)
        self.assertIsNone(progress.derive_tool_update("[download] 90%", transcribing))

    def test_cli_emit_is_opt_in(self) -> None:
        """Keep direct CLI interaction byte-for-byte free of GUI protocol records.

        Example: no progress environment variable means no printed marker.
        """

        with mock.patch.dict(os.environ, {}, clear=True), mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            progress.emit(progress.Stage.PREPARING, 0.0)
        self.assertEqual(out.getvalue(), "")

        with mock.patch.dict(os.environ, {progress.ENV_VIDEO_ID: "abc"}, clear=True), mock.patch(
            "sys.stdout",
            new_callable=io.StringIO,
        ) as out:
            progress.emit(progress.Stage.TRANSCRIBING, 0.25)
        update = progress.parse(out.getvalue().strip())
        self.assertEqual(update, progress.make("abc", progress.Stage.TRANSCRIBING, 0.25))


if __name__ == "__main__":
    unittest.main()
