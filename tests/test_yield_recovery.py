"""Exercise failed yield updates through retries and validated cache reuse.

Example: `python -m unittest tests.test_yield_recovery` never calls OpenAI.
"""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from yt_whisper_subs import chapters
from yt_whisper_subs import cli
from yt_whisper_subs import library_artifacts
from yt_whisper_subs import library_types as types
from yt_whisper_subs import media
from yt_whisper_subs import output_lock
from yt_whisper_subs import pipeline
from yt_whisper_subs import subtitle_files
from yt_whisper_subs import task_cancel
from yt_whisper_subs import yield_files
from tests import test_library as fixtures
from tests import test_subtitle_files


def cue(text: str) -> str:
    """Build one usable subtitle whose meaning can change across revisions.

    Example: `cue("New subject")` shares timings with the previous transcript.
    """

    return f"1\n00:00:00,000 --> 00:00:02,000\n{text}\n\n"


class YieldRecoveryTests(unittest.TestCase):
    """Run the real orchestration around mocked expensive generation.

    Example: instantiate a new runner after failure to model an app restart.
    """

    def setUp(self) -> None:
        """Prepare valid old English/chapters beside an unusable primary pair.

        Example: all outputs belong to a tiny temporary library.
        """

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.video = self.root / "videos" / "aaaaaaaaaaa.mkv"
        self.video.parent.mkdir()
        self.video.write_bytes(b"video")
        with mock.patch.object(sys, "argv", ["test", "--video-file", str(self.video),
                "--chapters", "--no-play", "--compact-subs", "none", "--subtitle-gap-extension", "0"]):
            self.args = cli.parse_args()
        self.runner = self._runner()
        self.runner._dirs.create()
        self.yields = self.runner._build_yields(self.video, None)
        self.yields.primary.sidecar.write_bytes(b"\0")
        self.yields.primary.archive.write_bytes(b"\0")
        self.yields.english.sidecar.write_text(cue("Old English"), encoding="utf-8")
        self.yields.english.sync_archive()
        self.yields.chapters.write(self._plan("Old chapter"))
        self.enterContext(redirect_stdout(io.StringIO()))
        self.enterContext(mock.patch.object(pipeline.PipelineRunner, "_ensure_whisper_ready"))
        self.enterContext(mock.patch.object(media, "extract_audio"))
        self.enterContext(mock.patch.object(media, "probe_duration_ms", return_value=90_000))
        self.whisper = self.enterContext(mock.patch.object(pipeline.whisper_local, "run_whisper", side_effect=self._transcribe))

    def _runner(self) -> pipeline.PipelineRunner:
        """Use a fresh orchestration owner with the same durable yields.

        Example: `_runner().run()` observes stale markers after a failed run.
        """

        return pipeline.PipelineRunner(self.args, {}, self.root, self.root / "run.log")

    def _plan(self, title: str) -> chapters.ChapterSet:
        """Create structurally valid old or replacement chapter content.

        Example: a title change proves the retry replaced the old plan.
        """

        return chapters.ChapterSet("aaaaaaaaaaa", "nl", "mock", 1, 90_000,
            [chapters.Chapter(0, title, title)])

    def _transcribe(self, _audio, target, *_args, **_kwargs) -> None:
        """Commit a usable replacement primary at the Whisper boundary.

        Example: the real pair acceptance/finalization runs afterward.
        """

        target.write_text(cue("New Dutch"), encoding="utf-8")

    def _translate(self, _primary, target, _args) -> None:
        """Supply usable replacement text without an API call.

        Example: the output differs from `Old English` even with equal timings.
        """

        target.write_text(cue("New English"), encoding="utf-8")

    def _chapter(self, _source, files, _args) -> chapters.ChapterSet:
        """Persist the replacement plan through its normal file boundary.

        Example: successful writing clears the chapter stale marker.
        """

        plan = self._plan("New chapter")
        files.write(plan)
        return plan

    def test_translation_failure_retries_only_stale_dependents_after_restart(self) -> None:
        """Never accept old downstream text after a new primary succeeds.

        Example: the restarted run translates once and regenerates chapters.
        """

        with mock.patch.object(pipeline.openai_translate, "translate_srt_with_openai", side_effect=RuntimeError("API failed")):
            with self.assertRaisesRegex(RuntimeError, "API failed"):
                self.runner.run()
        self.assertFalse(self.yields.all_ready())
        self.assertIsNone(self.yields.chapters.load())
        self.assertIn("Old English", self.yields.english.sidecar.read_text())
        with mock.patch.object(pipeline.openai_translate, "translate_srt_with_openai", side_effect=self._translate) as translate, mock.patch.object(pipeline.openai_chapters, "generate_chapters", side_effect=self._chapter) as generate:
            self.assertEqual(self._runner().run(), 0)
        self.whisper.assert_called_once()
        translate.assert_called_once()
        generate.assert_called_once()
        self.assertTrue(self.yields.all_ready())
        self.assertIn("New English", self.yields.english.archive.read_text())
        self.assertEqual(self.yields.chapters.load().chapters[0].english_title, "New chapter")

    def test_chapter_failure_retries_chapters_without_retranslating(self) -> None:
        """Retain durable chapter invalidation after English has succeeded.

        Example: a failed plan cannot become reusable merely because JSON parses.
        """

        with mock.patch.object(pipeline.openai_translate, "translate_srt_with_openai", side_effect=self._translate), mock.patch.object(pipeline.openai_chapters, "generate_chapters", side_effect=RuntimeError("chapter failed")):
            with self.assertRaisesRegex(RuntimeError, "chapter failed"):
                self.runner.run()
        self.assertTrue(self.yields.english.ready())
        self.assertFalse(self.yields.chapters.ready())
        with mock.patch.object(pipeline.openai_translate, "translate_srt_with_openai") as translate, mock.patch.object(pipeline.openai_chapters, "generate_chapters", side_effect=self._chapter) as generate:
            self.assertEqual(self._runner().run(), 0)
        translate.assert_not_called()
        generate.assert_called_once()
        self.whisper.assert_called_once()

    def test_unrequested_dependents_remain_stale_after_primary_repair(self) -> None:
        """Preserve dependency invalidation even when a run skips those stages.

        Example: `--no-english-for-dutch` cannot silently validate an old plan.
        """

        self.args.english_for_dutch = False
        self.args.chapters = False
        self.assertEqual(self.runner.run(), 0)
        self.assertFalse(self.yields.english.ready())
        self.assertFalse(self.yields.chapters.ready())
        self.assertIn("Old English", self.yields.english.sidecar.read_text())

    def test_stale_marker_invalidates_cached_gui_health(self) -> None:
        """Show pending regeneration despite unchanged, usable subtitle bytes.

        Example: old English receives Repair rather than Complete.
        """

        self.yields.primary.sidecar.write_text(cue("Dutch"), encoding="utf-8")
        record = types.VideoRecord(fixtures.make_meta("aaaaaaaaaaa", "Example"), None, 1,
            types.LocalMedia(self.video, 1, 5), None, None)
        cache = library_artifacts.ArtifactCache(self.root)
        self.assertIsNone(cache.issue(record))
        self.yields.english.invalidate()
        self.assertEqual(cache.issue(record), "English subtitles need regeneration")

    def test_playback_releases_pipeline_write_ownership(self) -> None:
        """Allow another pipeline while a completed CLI video is playing.

        Example: mpv does not keep the heavy processing lane locked.
        """

        self.yields.primary.sidecar.write_text(cue("Dutch"), encoding="utf-8")
        self.yields.primary.sync_archive()
        self.args.no_play = False
        def play(_yields, chapter_path):
            """Probe write ownership while playback is invoked.

            Example: acquiring the same lock succeeds after finalization.
            """
            with output_lock.OutputLock(self.root, "pipeline"):
                pass
            self.assertEqual(chapter_path, self.yields.chapters.mpv)
        with mock.patch.object(self.runner, "_ensure_requested_tools"), mock.patch.object(self.runner, "_play", side_effect=play) as player:
            self.assertEqual(self.runner.run(), 0)
        player.assert_called_once()
        self.whisper.assert_not_called()

    def test_stale_chapter_marker_invalidates_cached_plan(self) -> None:
        """Hide obsolete chapters even when the archive bytes have not changed.

        Example: the inspector cannot retain a pre-repair cached chapter plan.
        """

        cache = library_artifacts.ArtifactCache(self.root)
        self.assertIsNotNone(cache.chapter_set("aaaaaaaaaaa"))
        self.yields.chapters.invalidate()
        self.assertIsNone(cache.chapter_set("aaaaaaaaaaa"))


class SubtitleRepairTests(unittest.TestCase):
    """Preserve usable copies during corrupt-input repair and failed writes.

    Example: canonical archive content outranks an obsolete backup.
    """

    def test_valid_archive_repairs_invalid_sidecar_before_transforms(self) -> None:
        """Recover from valid canonical content without retranscription.

        Example: NUL bytes and an older backup cannot overwrite the archive.
        """

        with tempfile.TemporaryDirectory() as tmp:
            pair = subtitle_files.SubtitlePair(Path(tmp) / "video.srt", Path(tmp) / "archive.srt")
            pair.sidecar.write_bytes(b"\0")
            pair.archive.write_text(cue("Current archive"), encoding="utf-8")
            subtitle_files.uncompacted_backup_path(pair.sidecar).write_text(cue("Old backup"), encoding="utf-8")
            args = test_subtitle_files.SubtitlePairTests()._args(compact_subs="all")
            pair.hydrate("primary", args, is_english=False, force=False)
            pair.ensure_compacted(args, is_english=False, label="primary", force=False)
            self.assertTrue(pair.ready())
            self.assertIn("Current archive", pair.sidecar.read_text())
            self.assertIn("Current archive", pair.archive.read_text())

    def test_invalid_replacement_preserves_archive_and_backups(self) -> None:
        """Reject broken output before discarding any usable stored copy.

        Example: an empty generator result retains both archive and backup.
        """

        with tempfile.TemporaryDirectory() as tmp:
            pair = subtitle_files.SubtitlePair(Path(tmp) / "video.srt", Path(tmp) / "archive.srt")
            pair.sidecar.write_bytes(b"\0")
            pair.archive.write_text(cue("Saved"), encoding="utf-8")
            backup = subtitle_files.uncompacted_backup_path(pair.archive)
            backup.write_text(cue("Backup"), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "previous archive was preserved"):
                pair.accept_sidecar_replacement()
            pair.sync_archive()
            self.assertIn("Saved", pair.archive.read_text())
            self.assertTrue(backup.exists())

    def test_partial_copy_failure_does_not_replace_target(self) -> None:
        """Keep the previous archive even if copying its replacement fails.

        Example: a full disk leaves only the old canonical archive.
        """

        with tempfile.TemporaryDirectory() as tmp:
            source, target = Path(tmp) / "source.srt", Path(tmp) / "archive.srt"
            source.write_text(cue("New"), encoding="utf-8")
            target.write_text(cue("Saved"), encoding="utf-8")
            def failed_copy(_source, stage):
                """Leave incomplete staging bytes before reporting a write failure.

                Example: the canonical target must remain untouched.
                """
                stage.write_bytes(b"partial")
                raise OSError("disk full")
            with mock.patch.object(yield_files.shutil, "copy2", side_effect=failed_copy):
                with self.assertRaises(OSError):
                    yield_files.atomic_copy(source, target)
            self.assertIn("Saved", target.read_text())
            self.assertEqual(set(Path(tmp).iterdir()), {source, target})


class AudioRecoveryTests(unittest.TestCase):
    """Verify staged extraction, cache provenance, and failure preservation.

    Example: a partial cache without successful extraction evidence is rebuilt.
    """

    def setUp(self) -> None:
        """Isolate source, cached audio, and mock ffmpeg/ffprobe work.

        Example: source revisions differ by content length without real media.
        """

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.video, self.audio = self.root / "video.mkv", self.root / "video.opus"
        self.video.write_bytes(b"video")
        self.run = self.enterContext(mock.patch.object(media.proc, "run", side_effect=self._extract))
        self.enterContext(mock.patch.object(media, "probe_duration_ms", return_value=90_000))
        self.enterContext(mock.patch.object(media, "probe_audio_duration_ms", return_value=90_000))

    def _extract(self, command, **_kwargs):
        """Write complete mock audio into the supplied staging pathname.

        Example: the canonical audio is unchanged until promotion.
        """

        Path(command[-1]).write_bytes(b"complete audio")

    def test_cancelled_extraction_does_not_publish_or_reuse_partial_audio(self) -> None:
        """Retry extraction after cancellation rather than trusting leftover bytes.

        Example: the staging file is removed and the next ffmpeg call runs.
        """

        def cancelled(command, **kwargs):
            """Create partial staging bytes, then emulate the stop hook.

            Example: no canonical audio should be promoted.
            """
            self._extract(command)
            raise task_cancel.CancelledError("cancelled")
        self.run.side_effect = cancelled
        with self.assertRaises(task_cancel.CancelledError):
            media.extract_audio(self.video, self.audio, "opus", False)
        self.assertFalse(self.audio.exists())
        self.assertEqual(list(self.root.iterdir()), [self.video])
        self.run.side_effect = self._extract
        media.extract_audio(self.video, self.audio, "opus", False)
        self.assertEqual(self.run.call_count, 2)

    def test_unvalidated_cache_is_rebuilt_then_reused(self) -> None:
        """Require successful validation evidence before existence implies reuse.

        Example: a previous version's truncated audio is rebuilt once.
        """

        self.audio.write_bytes(b"partial")
        media.extract_audio(self.video, self.audio, "opus", False)
        media.extract_audio(self.video, self.audio, "opus", False)
        self.run.assert_called_once()
        self.assertEqual(self.audio.read_bytes(), b"complete audio")

    def test_source_or_audio_change_invalidates_cache(self) -> None:
        """Bind extraction evidence to both source and output revisions.

        Example: replacing a short replay or truncating audio reruns extraction.
        """

        media.extract_audio(self.video, self.audio, "opus", False)
        self.video.write_bytes(b"longer replacement video")
        media.extract_audio(self.video, self.audio, "opus", False)
        self.audio.write_bytes(b"x")
        media.extract_audio(self.video, self.audio, "opus", False)
        self.assertEqual(self.run.call_count, 3)

    def test_failed_force_preserves_valid_audio_and_evidence(self) -> None:
        """Do not discard a working cache when a requested replacement fails.

        Example: retry without force still reuses the previously validated audio.
        """

        media.extract_audio(self.video, self.audio, "opus", False)
        self.run.side_effect = RuntimeError("ffmpeg failed")
        with self.assertRaises(RuntimeError):
            media.extract_audio(self.video, self.audio, "opus", True)
        media.extract_audio(self.video, self.audio, "opus", False)
        self.assertEqual(self.run.call_count, 2)
        self.assertEqual(self.audio.read_bytes(), b"complete audio")

    def test_short_extraction_is_not_promoted(self) -> None:
        """Reject a completed-looking output that does not cover the source.

        Example: 20-second audio cannot replace a 90-second source extraction.
        """

        with mock.patch.object(media, "probe_duration_ms", return_value=20_000):
            with self.assertRaisesRegex(RuntimeError, "duration does not match"):
                media.extract_audio(self.video, self.audio, "opus", False)
        self.assertFalse(self.audio.exists())

    def test_short_audio_track_with_long_video_tail_is_valid(self) -> None:
        """Compare the extracted track with audio rather than container duration.

        Example: silence after the source audio ends must not block transcription.
        """

        with mock.patch.object(media, "probe_audio_duration_ms", return_value=20_000), mock.patch.object(media, "probe_duration_ms", return_value=20_000):
            media.extract_audio(self.video, self.audio, "opus", False)
        self.assertTrue(self.audio.exists())

    def test_delete_audio_also_discards_validation_evidence(self) -> None:
        """Keep optional cache cleanup complete.

        Example: `--delete-audio` leaves only the source video.
        """

        media.extract_audio(self.video, self.audio, "opus", False)
        media.remove_audio(self.audio)
        self.assertEqual(list(self.root.iterdir()), [self.video])
