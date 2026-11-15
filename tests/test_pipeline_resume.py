"""Verify retries preserve finalized subtitle timing and paid translation checkpoints.

Example: a long silent gap must not add another five seconds to a cached cue on every retry.
"""

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

from tests import test_subtitle_files as fixtures
from yt_whisper_subs import openai_translate
from yt_whisper_subs import pipeline
from yt_whisper_subs import srt
from yt_whisper_subs import subtitle_files


class PipelineResumeTests(unittest.TestCase):
    """Use actual file hydration and finalization; e.g. preserving timestamps must keep a matching checkpoint reusable."""

    def setUp(self):
        """Create newly finalized cues with silence longer than the extension; e.g. replay used to increase six seconds to eleven."""

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.args = fixtures.SubtitlePairTests()._args(compact_subs="none")
        self.args.force = False
        self.args.subtitle_gap_extension = 5
        self.primary = subtitle_files.SubtitlePair(self.root / "nl.srt", self.root / "nl-archive.srt")
        self.english = subtitle_files.SubtitlePair(self.root / "en.srt", self.root / "en-archive.srt")
        self.enterContext(redirect_stdout(io.StringIO()))
        for pair in (self.primary, self.english):
            pair.sidecar.write_text("1\n00:00:00,000 --> 00:00:01,000\nFirst.\n\n"
                                    "2\n00:00:30,000 --> 00:00:31,000\nNext.\n", encoding="utf-8")
            pair.finalize(self.args, is_english=pair == self.english, label="test")
        self.yields = SimpleNamespace(primary=self.primary, english=self.english, make_english=True)
        self.runner = pipeline.PipelineRunner(self.args, {}, self.root, self.root / "test.log")

    def test_retry_reuses_reviewed_checkpoint_without_moving_source_timings(self):
        """Keep checkpoint identity stable across failed translations; e.g. the first reviewed chunk costs no new request on retry."""

        source = self.primary.sidecar.read_text(encoding="utf-8")
        cues = srt.parse_srt(source)
        checkpoint = openai_translate.TranslationCheckpoint.for_run(source, cues, self.english.sidecar, self.args, 1)
        checkpoint.save(["First reviewed translation.", None])
        self.english.invalidate()
        for _ in range(3):
            self.runner._prepare_existing_subtitles(self.yields)
            self.assertEqual(self.primary.sidecar.read_text(encoding="utf-8"), source)
            self.assertEqual(self.primary.archive.read_text(encoding="utf-8"), source)
            current = openai_translate.TranslationCheckpoint.for_run(source, cues, self.english.sidecar, self.args, 1)
            self.assertEqual(current.load(), ["First reviewed translation.", None])
        self.assertEqual(srt.parse_srt(source)[0].end_ms, 6000)

    def test_cached_primary_and_whisper_english_are_not_extended_again(self):
        """Preserve both independent subtitle timelines on playback; e.g. cached Whisper English also gets only one gap extension."""

        self.args.english_translation_provider = "whisper"
        before = {path: path.read_bytes() for pair in (self.primary, self.english) for path in pair}
        for _ in range(3):
            self.runner._prepare_existing_subtitles(self.yields)
        self.assertTrue(all(path.read_bytes() == content for path, content in before.items()))
