"""Verify occurrence persistence, contextual translation, and nonblocking capture.

Example: `python -m unittest tests.test_vocabulary` makes no API calls.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from yt_whisper_subs import library_db
from yt_whisper_subs import cli
from yt_whisper_subs import pipeline
from yt_whisper_subs import playback
from yt_whisper_subs import vocabulary as words
from yt_whisper_subs import vocabulary_playback
from yt_whisper_subs import vocabulary_translate
from yt_whisper_subs import vocabulary_worker


MEANING = words.Meaning("walked", "lopen", "to walk", "verb", "past tense", "", False)


def wait_for(predicate, timeout: float = 3) -> None:
    """Wait for a meaningful async condition; e.g. a save completes while HTTP is blocked."""

    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("background vocabulary work did not complete")
        time.sleep(0.01)


class VocabularyTests(unittest.TestCase):
    """Keep every intentional click while deduplicating transport and lookup work."""

    def setUp(self) -> None:
        """Create an isolated real SQLite catalog; e.g. no user's vocabulary is touched."""

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._root = Path(self._tmp.name)
        self._db = library_db.LibraryDb(self._root / "catalog.sqlite3")
        self._db.initialize()
        self._path = self._root / "sample.srt"
        self._path.write_text("1\n00:00:01,000 --> 00:00:05,000\nIk liep, liep naar huis.\n\n"
                              "2\n00:00:06,000 --> 00:00:09,000\nIk was geïnteresseerd.\n", encoding="utf-8")
        self._transcript = words.Transcript.load(self._path, "nl")
        self._translator = vocabulary_translate.WordTranslator()

    def _selection(self, event_id: str, source: str = "first", cue: int = 0, token: int = 1) -> words.Selection:
        """Build a capture from authoritative tokens; e.g. repeated `liep` in two videos."""

        media = words.Source(f"youtube:{source}", source, f"Video {source}", f"https://youtu.be/{source}", "video.mkv")
        return words.Selection(media, self._transcript.context(cue, token), 2.75, time.time(), event_id)

    def _save(self, event_id: str, source: str = "first") -> dict:
        """Use the production context cache key; e.g. different videos can share one lookup."""

        selection = self._selection(event_id, source)
        return self._db.save_word(selection, self._translator.cache_identity(selection.context))

    def test_history_retains_repeats_and_distinct_video_counts(self) -> None:
        """Count intentional repeats independently of shared translations and transport replays."""

        first = self._save("a")
        self._save("a")
        self._save("b")
        other = self._save("c", "second")
        self.assertEqual(first["id"], other["id"])
        summary = self._db.word_summary()[0]
        self.assertEqual((summary["clicks"], summary["videos"]), (3, 2))
        history = self._db.word_history("nl", "liep")
        self.assertEqual(len(history), 3)
        self.assertEqual(history[0]["surface"], "liep")
        self.assertEqual(history[0]["position_seconds"], 2.75)
        self.assertEqual((history[0]["cue_start_ms"], history[0]["cue_end_ms"]), (1000, 5000))
        self.assertEqual(self._db.word_marks("youtube:first"), {("nl", 1000, "Ik liep, liep naar huis.", 3)})
        with self._db._connect() as conn:
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")

    def test_unicode_and_repeated_token_offsets_are_exact(self) -> None:
        """Disambiguate duplicate words and accents; e.g. select the second `liep`."""

        ctx = self._transcript.context(0, 2)
        self.assertEqual(ctx.surface, "liep")
        self.assertEqual(ctx.token.start, 9)
        self.assertEqual(self._transcript.context(1, 2).surface, "geïnteresseerd")
        self.assertEqual(words.normalized("HÉÉL"), words.normalized("héél"))
        first = self._translator.cache_identity(self._transcript.context(0, 1))
        self.assertNotEqual(first, self._translator.cache_identity(ctx))

    def test_failed_translation_preserves_click_and_can_be_retried(self) -> None:
        """Keep storage independent of network state; e.g. fix a missing API key later."""

        saved = self._save("a")
        self._db.claim_word_lookup("owner")
        self._db.finish_word_lookup(saved["id"], "owner", "network unavailable")
        self.assertEqual(self._db.word_summary()[0]["failed"], 1)
        self.assertEqual(self._db.retry_word_lookups(), 1)
        claimed = self._db.claim_word_lookup("next")
        self._db.finish_word_lookup(claimed["id"], "next", MEANING)
        result = self._db.word_summary()[0]
        self.assertEqual((result["clicks"], result["meaning"], result["lemma"]), (1, "walked", "lopen"))
        self.assertEqual(self._db.word_summary(by_lemma=True)[0]["word_key"], "lopen")

    def test_lease_excludes_concurrent_workers_and_late_completions(self) -> None:
        """Recover expired jobs without allowing old requests to overwrite current results."""

        saved = self._save("a")
        self.assertIsNotNone(self._db.claim_word_lookup("old"))
        self.assertIsNone(self._db.claim_word_lookup("new"))
        with self._db._connect() as conn:
            conn.execute("UPDATE vocab_lookups SET lease_until=0")
        self.assertIsNotNone(self._db.claim_word_lookup("new"))
        self._db.finish_word_lookup(saved["id"], "old", "late failure")
        self.assertEqual(self._db.word_lookup(saved["id"])["state"], "working")
        self._db.finish_word_lookup(saved["id"], "new", MEANING)
        self.assertEqual(self._db.word_lookup(saved["id"])["state"], "ready")

    def test_changed_sentence_never_reuses_a_spelling_only_translation(self) -> None:
        """Include context in cache identity; e.g. `bank` can have different meanings."""

        selection = self._selection("a")
        first = self._translator.cache_identity(selection.context)
        cue = selection.context.cue._replace(text="Ik liep naar de bank.")
        changed = selection.context._replace(cue=cue)
        self.assertNotEqual(first, self._translator.cache_identity(changed))

    def test_save_lane_is_independent_of_blocked_http(self) -> None:
        """Acknowledge all committed clicks before translation; e.g. a slow API request."""

        entered, release = threading.Event(), threading.Event()
        translator = mock.Mock(wraps=self._translator)
        translator.model, translator.reasoning = self._translator.model, self._translator.reasoning

        def blocked(_document):
            """Hold network completion until local captures are verified; e.g. no UI stall."""

            entered.set()
            release.wait(3)
            return MEANING

        translator.translate.side_effect = blocked
        service = vocabulary_worker.VocabularyService(self._db, translator)
        replies = []
        try:
            service.submit(self._selection("a"), replies.append)
            self.assertTrue(entered.wait(2))
            service.submit(self._selection("b", "second"), replies.append)
            wait_for(lambda: len(replies) >= 2)
            self.assertTrue(all(reply["saved"] for reply in replies[:2]))
            self.assertEqual(self._db.word_summary()[0]["clicks"], 2)
            release.set()
            wait_for(lambda: self._db.word_summary()[0]["state"] == "ready")
            self.assertEqual(translator.translate.call_count, 1)
        finally:
            release.set()
            service.close()
            if service._threads:
                service._threads[1].join(3)

    def test_close_drains_accepted_saves(self) -> None:
        """Never lose a click because mpv closes immediately; e.g. click then quit."""

        translator = mock.Mock(wraps=self._translator)
        translator.model, translator.reasoning = self._translator.model, self._translator.reasoning
        translator.translate.return_value = MEANING
        service = vocabulary_worker.VocabularyService(self._db, translator)
        for idx in range(8):
            service.submit(self._selection(str(idx)), lambda _: None)
        service.close()
        service._threads[1].join(3)
        self.assertEqual(self._db.word_summary()[0]["clicks"], 8)

    def test_player_capture_rejects_bad_indices_and_binds_source(self) -> None:
        """Validate messages against the launch transcript; e.g. another GUI selection is irrelevant."""

        service = mock.Mock()
        service.marks.return_value = set()
        source = self._selection("a").source
        capture = vocabulary_playback.WordCapture(service, source)
        capture.prepare([self._path], [self._path], playback.PlaybackPrefs.defaults())
        send = mock.Mock()
        capture.connected(send)
        payload = {"request": 1, "track": 1, "cue": 1, "token": 2, "position": 2.5}
        capture.receive(["vocab-click", json.dumps(payload)])
        saved = service.submit.call_args.args[0]
        self.assertEqual((saved.source, saved.context.surface, saved.position), (source, "liep", 2.5))
        service.submit.reset_mock()
        for change in ({"cue": -1}, {"token": 100}, {"request": True}, {"position": float("nan")}):
            capture.receive(["vocab-click", json.dumps(payload | change)])
        service.submit.assert_not_called()

    @mock.patch("yt_whisper_subs.openai_client.responses_api_request")
    def test_translation_payload_is_contextual_and_schema_validated(self, request) -> None:
        """Use the existing client and no media paths; e.g. translate a selected token only."""

        request.return_value = {"output_text": json.dumps(MEANING._asdict())}
        ctx = self._transcript.context(0, 2)
        result = self._translator.translate(ctx.document())
        payload = request.call_args.args[1]
        self.assertEqual(result, MEANING)
        self.assertTrue(payload["text"]["format"]["strict"])
        self.assertFalse(payload["store"])
        self.assertEqual(json.loads(payload["input"])["span"], [9, 13])
        self.assertNotIn("video.mkv", json.dumps(payload))
        request.return_value = {"output_text": '{"meaning":"walked"}'}
        with self.assertRaises(ValueError):
            self._translator.translate(ctx.document())

    @mock.patch("yt_whisper_subs.playback.play_video")
    def test_cli_playback_shares_capture_and_output_database(self, play) -> None:
        """Enable the same word feature for local CLI media; e.g. no separate vocabulary store."""

        video = self._root / "sample.mkv"
        video.write_bytes(b"test")
        with mock.patch("sys.argv", ["yt_whisper_subs.py", "--video-file", str(video)]):
            args = cli.parse_args()
        yields = mock.Mock(video=video)
        yields.srt_paths.return_value = [self._path]
        runner = pipeline.PipelineRunner(args, {}, self._root, self._root / "run.log")
        runner._play(yields, None)
        session = play.call_args.args[3]
        self.assertTrue(session.words.source_key.startswith("file:"))
        self.assertTrue((self._root / "library" / "catalog.sqlite3").is_file())


if __name__ == "__main__":
    unittest.main()
