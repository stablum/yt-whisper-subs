"""Tests for subtitle sidecar/archive pair behavior.

Example: `python -m unittest tests.test_subtitle_files`.
"""

from __future__ import annotations

import argparse
import tempfile
import unittest
from pathlib import Path

from yt_whisper_subs import srt
from yt_whisper_subs import subtitle_files


class SubtitlePairTests(unittest.TestCase):
    """Cover the cohesive sidecar/archive subtitle pair object.

    Example: `SubtitlePairTests("test_hydrate_copies_archive_and_syncs_sidecar")`.
    """

    def test_hydrate_copies_archive_and_syncs_sidecar(self) -> None:
        """Hydrate from either existing copy so both subtitle yields are ready.

        Example: archive first, then sidecar first.
        """

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            sidecar_path = root / "videos" / "x.srt"
            archive_path = root / "subtitles" / "x.srt"
            pair = subtitle_files.SubtitlePair(sidecar_path, archive_path)
            archive_path.parent.mkdir(parents=True)
            archive_path.write_text(self._srt_text("Archive copy"), encoding="utf-8")

            pair.hydrate("primary", self._args(compact_subs="none"), is_english=False, force=False)
            self.assertTrue(pair.ready())
            self.assertEqual(sidecar_path.read_text(encoding="utf-8"), archive_path.read_text(encoding="utf-8"))

            archive_path.unlink()
            sidecar_path.write_text(self._srt_text("Sidecar copy"), encoding="utf-8")
            pair.hydrate("primary", self._args(compact_subs="none"), is_english=False, force=False)
            self.assertTrue(pair.ready())
            self.assertIn("Sidecar copy", archive_path.read_text(encoding="utf-8"))

    def test_compaction_saves_uncompacted_backup_and_syncs_archive(self) -> None:
        """Compact both subtitle copies and preserve the original cue split.

        Example: two adjacent cues become one readable cue.
        """

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            sidecar_path = root / "videos" / "x.srt"
            archive_path = root / "subtitles" / "x.srt"
            pair = subtitle_files.SubtitlePair(sidecar_path, archive_path)
            sidecar_path.parent.mkdir(parents=True)
            archive_path.parent.mkdir(parents=True)
            content = (
                "1\n00:00:00,000 --> 00:00:01,000\nHallo\n\n"
                "2\n00:00:01,200 --> 00:00:02,000\nallemaal\n"
            )
            sidecar_path.write_text(content, encoding="utf-8")
            archive_path.write_text(content, encoding="utf-8")

            changed = pair.ensure_compacted(
                self._args(compact_subs="all"),
                is_english=False,
                label="primary",
                force=False,
            )

            self.assertTrue(changed)
            self.assertTrue(subtitle_files.uncompacted_backup_path(sidecar_path).exists())
            self.assertEqual(sidecar_path.read_text(encoding="utf-8"), archive_path.read_text(encoding="utf-8"))
            self.assertIn("Hallo allemaal", sidecar_path.read_text(encoding="utf-8"))

    def test_new_transcript_replaces_archive_and_stale_backups(self) -> None:
        """Make the new sidecar the sole source before final transforms run.

        Example: a repaired transcript cannot retain its hallucinated archive.
        """

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            pair = subtitle_files.SubtitlePair(root / "video.srt", root / "archive.srt")
            pair.sidecar.write_text("new", encoding="utf-8")
            pair.archive.write_text("stale", encoding="utf-8")
            backups = [subtitle_files.uncompacted_backup_path(path) for path in pair]
            for path in backups:
                path.write_text("stale", encoding="utf-8")

            pair.accept_sidecar_replacement()

            self.assertFalse(any(path.exists() for path in backups))
            self.assertEqual(pair.archive.read_text(encoding="utf-8"), "new")

    def _args(self, *, compact_subs: str) -> argparse.Namespace:
        """Build the option namespace used by subtitle pair transforms.

        Example: `self._args(compact_subs="all")`.
        """

        return argparse.Namespace(
            compact_subs=compact_subs,
            compact_primary_for_openai_translation=False,
            compact_soft_periods="none",
            english_translation_provider="openai",
            compact_gap=0.5,
            compact_max_duration=9.0,
            compact_max_chars=180,
            compact_max_cps=25.0,
            compact_line_width=50,
            subtitle_gap_extension=0.0,
        )

    def _srt_text(self, text: str) -> str:
        """Render one tiny SRT block for pair hydration tests.

        Example: `self._srt_text("Hello")`.
        """

        return f"1\n00:00:00,000 --> 00:00:01,000\n{text}\n"


class SubtitleQualityTests(unittest.TestCase):
    """Reject long speech-recognition loops without flagging ordinary repetition.

    Example: `SubtitleQualityTests("test_repeated_long_phrase_is_invalid")`.
    """

    def test_repeated_long_phrase_is_invalid(self) -> None:
        """Detect the exact 30-second repetition pattern from the reported video.

        Example: repeated station-ident cues become a repairable pipeline issue.
        """

        cues = [
            srt.SubtitleCue(idx * 30_000, (idx + 1) * 30_000, "TV Gelderland 2021")
            for idx in range(3)
        ]

        self.assertEqual(srt.repeated_phrase_loop(cues), "TV Gelderland 2021")

    def test_short_or_nonconsecutive_repetition_is_usable(self) -> None:
        """Allow normal repeated words unless they form one long consecutive loop.

        Example: several brief music labels do not condemn a complete transcript.
        """

        cues = [
            srt.SubtitleCue(0, 3_000, "Music"),
            srt.SubtitleCue(33_000, 36_000, "Music"),
            srt.SubtitleCue(66_000, 69_000, "Music"),
            srt.SubtitleCue(69_000, 72_000, "Speech"),
            srt.SubtitleCue(12_000, 42_000, "Music"),
            srt.SubtitleCue(42_000, 72_000, "Speech"),
            srt.SubtitleCue(72_000, 102_000, "Music"),
        ]

        self.assertIsNone(srt.repeated_phrase_loop(cues))

    def test_file_issue_explains_loop(self) -> None:
        """Return a user-facing reason rather than a generic invalid status.

        Example: the GUI can explain why Repair replaced Complete.
        """

        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "video.srt"
            content = "\n\n".join(
                f"{idx + 1}\n00:0{idx}:00,000 --> 00:0{idx + 1}:00,000\nStation ident"
                for idx in range(3)
            )
            path.write_text(content, encoding="utf-8")

            self.assertIn("repeated speech-recognition loop", srt.file_issue(path) or "")
            self.assertFalse(srt.file_is_usable(path))


if __name__ == "__main__":
    unittest.main()
