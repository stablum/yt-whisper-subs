"""Verify durable listening preparation, exact dictation and shared vocabulary review.

Example: tests use actual SQLite and scheduling, with offline sentence and speech providers.
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import listening_fixtures as audio
from tests import sentence_fixtures as fixtures
from yt_whisper_subs import library_db
from yt_whisper_subs import library_schema
from yt_whisper_subs import listening_tts
from yt_whisper_subs import listening_grade
from yt_whisper_subs import practice_activity
from yt_whisper_subs import practice_tracks
from yt_whisper_subs import pronunciation
from yt_whisper_subs import sentence_practice as sentences
from yt_whisper_subs import sentence_service
from yt_whisper_subs import sentence_stats


class ListeningTests(unittest.TestCase):
    """Check actual learning and failure boundaries; e.g. one misheard word does not fail unrelated targets."""

    def setUp(self) -> None:
        """Seed only synthetic source clicks; e.g. no private vocabulary or paid service is involved."""

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = library_db.LibraryDb(Path(tmp.name) / "catalog.sqlite3")
        self.db.initialize()
        fixtures.seed(self.db)
        self.speech, self.provider = audio.Speech(), fixtures.FakeSentenceProvider()
        self.engine = sentence_service.SentenceService(self.db, self.provider, speech=self.speech)
        self.opts = fixtures.options(direction=sentences.Direction.LISTEN)

    def _row(self, *, background=False) -> dict:
        """Prepare and open an audio-backed question; e.g. a session starts only after its recordings exist."""

        session = self.engine.start(self.opts, lambda _: None, background=background)
        return self.db.sentence_current(session)

    def test_audio_stock_survives_restart_without_regeneration_or_exposure(self) -> None:
        """Consume checked text and both speech speeds exactly once; e.g. background work never counts as practice."""

        self.engine.start(self.opts, lambda _: None, background=True)
        self.assertIsNone(self.db.sentence_active("listen_nl"))
        self.assertEqual(practice_activity.read(self.db).answers, 0)
        self.provider.available = self.speech.available = False
        reopened = sentence_service.SentenceService(self.db, self.provider, speech=self.speech)
        session = reopened.start(self.opts, lambda _: None)
        row = self.db.sentence_current(session)
        recording = self.db.listening_audio(row["question_id"])
        self.assertTrue(self.db.listening_audio_valid(recording))
        self.assertGreater(recording["slow_seconds"], recording["normal_seconds"])
        self.assertEqual((len(self.provider.calls), len(self.speech.calls)), (1, 1))

    def test_optional_pronunciation_reuses_speech_and_never_counts_as_an_answer(self) -> None:
        """Reuse a validated dictation take and cache only normal audio for optional replay."""

        report = lambda _: None
        stock = listening_tts.Recordings(self.db).prepare(self.speech, ['Ik lees.'], report)
        normal = self.db.listening_paths(stock['Ik lees.'])['normal']
        reader = pronunciation.Audio(self.db, speech=self.speech)
        texts = ['Ik lees. Ik schrijf.', 'Ik lees.']
        clips = reader.prepare(texts, report)
        self.assertEqual([path for path, _ in clips[::2]], [normal, normal])
        self.assertEqual([text for text, _ in self.speech.calls], ['Ik lees.', 'Ik schrijf.'])
        self.assertEqual(set(self.speech.calls[-1][1]), {'normal'})
        self.assertEqual(reader.prepare(texts, report), clips)
        self.assertEqual(reader.cached(texts), clips)
        self.assertIsNone(reader.cached(['Dit is nog niet opgenomen.']))
        self.assertEqual(len(self.speech.calls), 2)
        self.assertEqual(practice_activity.read(self.db).answers, 0)

    def test_transcription_is_local_reveals_english_and_counts_only_submitted_effort(self) -> None:
        """Ignore case and punctuation while retaining the spoken words; e.g. replays add assistance, not extra practice answers."""

        row = self._row()
        self.db.listening_play(row["id"], "normal")
        self.db.listening_play(row["id"], "slow")
        self.assertEqual(practice_activity.read(self.db).answers, 0)
        answer = fixtures.DUTCH.upper().replace(".", "!")
        self.db.sentence_submit(row["id"], answer, 2300)
        self.provider.available = False
        result = self.engine.check(row["id"], lambda _: None)
        self.assertEqual(result["outcome"], "correct")
        self.assertEqual(result["grade"]["corrected_meaning"], fixtures.ENGLISH)
        self.assertEqual(self.provider.graded, [])
        day = practice_activity.read(self.db).week[-1]
        self.assertEqual((day.answers, day.listening, day.sentences), (1, 1, 0))
        data = sentence_stats.snapshot(self.db, listening=True)
        self.assertEqual((data["graded"], data["partial_score"], data["listens"], data["slow_listens"]), (1, 1, 2, 1))
        self.assertEqual(sentence_stats.snapshot(self.db)["graded"], 0)
        self.engine.check(row["id"], lambda _: None)
        with self.db._connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM word_transfers").fetchone()[0], 3)
            self.assertFalse(conn.execute("PRAGMA foreign_key_check").fetchall())

    def test_misheard_word_updates_shared_recognition_without_failing_other_targets(self) -> None:
        """Treat a synonym as different spoken wording and retain partial progress; e.g. intelligentste was not the pronounced slimste."""

        row = self._row()
        answer = fixtures.DUTCH.replace("slimste", "intelligentste")
        self.db.sentence_submit(row["id"], answer, 2100)
        result = self.engine.check(row["id"], lambda _: None)
        failed = [word for word in result["grade"]["words"] if not word["meaning_correct"]]
        self.assertEqual(len(failed), 1)
        candidates = {row["word_id"]: row for row in self.db.word_candidates("nl_en")}
        self.assertEqual(candidates[failed[0]["word_id"]]["failures"], 1)
        self.assertTrue(all(row["failures"] == 0 for key, row in candidates.items() if key != failed[0]["word_id"]))
        score = sentence_stats.snapshot(self.db, listening=True)["partial_score"]
        self.assertGreater(score, 0.8)
        self.assertLess(score, 1)
        self.db.sentence_flag(row["id"])
        self.assertTrue(all(row["failures"] == 0 for row in self.db.word_candidates("nl_en")))

    def test_alignment_handles_omissions_additions_and_supporting_word_changes(self) -> None:
        """Retain precise target evidence across different transcription errors; e.g. mishearing buurman does not fail slimste."""

        content = self._row()["content"]
        for answer in (fixtures.DUTCH.replace("slimste ", ""), fixtures.DUTCH + " Echt waar.",
                       fixtures.DUTCH.replace("buurman", "vriend"), "Ik hoorde iets anders."):
            with self.subTest(answer=answer):
                grade = listening_grade.grade(answer, content)
                self.assertFalse(grade.correct)
                self.assertEqual(grade.corrected, fixtures.DUTCH)
                self.assertTrue(grade.errors)
        grade = listening_grade.grade(fixtures.DUTCH.replace("buurman", "vriend"), content)
        self.assertTrue(all(word.correct for word in grade.words))

    def test_startup_rebuilds_shared_listening_reviews_in_the_recognition_direction(self) -> None:
        """Recover derived shared review state from immutable dictation evidence; e.g. a startup replay must not invent a third quiz direction."""

        row = self._row()
        self.db.sentence_submit(row["id"], fixtures.DUTCH.replace("slimste", "slimmere"), 1100)
        self.engine.check(row["id"], lambda _: None)
        with self.db._connect() as conn:
            conn.execute("DELETE FROM word_transfers")
            conn.execute("DELETE FROM practice_states")
        self.db.initialize()
        candidates = self.db.word_candidates("nl_en")
        self.assertEqual(sum(row["failures"] for row in candidates), 1)
        self.assertEqual(sum(row["attempts"] for row in candidates), 3)
        self.assertEqual(self.db.word_candidates("en_nl")[0]["attempts"], 0)

    def test_speech_failure_and_deleted_cache_reuse_approved_question_text(self) -> None:
        """Repair audio without purchasing a replacement question; e.g. a failed download cannot strand its unused approvals."""

        self.speech.available = False
        with self.assertRaises(OSError):
            self.engine.start(self.opts, lambda _: None, background=True)
        self.assertIsNone(self.db.sentence_active())
        self.speech.available = True
        row = self._row()
        self.assertEqual(len(self.provider.calls), 1)
        recording = self.db.listening_audio(row["question_id"])
        paths = self.db.listening_paths(recording["cache_key"])
        paths["slow"].unlink()
        # An unexposed foreground round follows the same materialization path on reuse.
        with self.db._connect() as conn:
            conn.execute("UPDATE sentence_presentations SET shown_at=NULL WHERE id=?", (row["id"],))
        self.engine.start(self.opts, lambda _: None)
        self.assertTrue(paths["slow"].is_file())
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(set(self.speech.calls[-1][1]), {"slow"})

    def test_migration_preserves_pending_answer_draft_and_foreign_keys(self) -> None:
        """Upgrade a genuine pre-listening schema without changing learner history; e.g. queued translations remain correctable."""

        path = self.db.path.with_name("old.sqlite3")
        old = library_db.LibraryDb(path)
        schema = library_schema._SCHEMA.replace("'nl_en','en_nl','listen_nl'", "'nl_en','en_nl'")
        with mock.patch.object(library_schema, "_SCHEMA", schema):
            old.initialize()
        fixtures.seed(old)
        engine = sentence_service.SentenceService(old, self.provider, speech=self.speech)
        row = old.sentence_current(engine.start(fixtures.options(), lambda _: None))
        old.sentence_submit(row["id"], "Mijn bestaande antwoord", 1200)
        before = old.sentence_get(row["id"])
        old.initialize()
        self.assertEqual(old.sentence_get(row["id"]), before)
        engine.start(self.opts, lambda _: None)
        with old._connect() as conn:
            self.assertFalse(conn.execute("PRAGMA foreign_key_check").fetchall())
            self.assertEqual(conn.execute("SELECT state FROM sentence_checks WHERE presentation_id=?", (row["id"],)).fetchone()[0], "queued")

    def test_corrupt_speech_is_rebuilt_and_duplicate_text_is_synthesized_once(self) -> None:
        """Check recording identity as well as playable headers; e.g. altered speech cannot be taught from a stale metadata row."""

        self.opts = self.opts.model_copy(update={"count": 2})
        self.engine.start(self.opts, lambda _: None, background=True)
        self.assertEqual(len(self.speech.calls), 1)
        policy = {"model": self.provider.model, "reasoning": self.provider.reasoning, "prompt_version": self.engine.start.prompt_version}
        rows = self.db.practice_stock(practice_tracks.Track.LISTENING, self.opts, policy)
        cached = self.db.listening_audio(rows[0]["id"])
        path = self.db.listening_paths(cached["cache_key"])["normal"]
        raw = path.read_bytes()
        path.write_bytes(raw[:-2] + b"\x01\x01")
        self.assertFalse(self.db.listening_audio_valid(cached))
        self.engine.start(self.opts, lambda _: None)
        self.assertTrue(self.db.listening_audio_valid(self.db.listening_audio(rows[0]["id"])))
        self.assertEqual(len(self.provider.calls), 1)

    def test_native_speech_failure_is_reported_and_manifest_is_cleaned(self) -> None:
        """Contain native runtime exits in the child process; e.g. a failed espeak initialization cannot terminate the GUI host."""

        speech = listening_tts.PiperSpeech(self.db.path.parent)
        speech._model = self.db.path.parent / "verified-test-voice.onnx"
        paths = {"normal": self.db.path.parent / "normal.wav"}
        failed = subprocess.CompletedProcess([], 1, stdout="Native phonemizer failed")
        with mock.patch.object(listening_tts.proc, "run", return_value=failed):
            with self.assertRaisesRegex(RuntimeError, "phonemizer failed"):
                speech.render(fixtures.DUTCH, paths, lambda _: None)
        self.assertFalse(list(self.db.path.parent.glob("synthesis-*.json")))
        self.assertFalse(paths["normal"].exists())

    def test_one_audio_repair_updates_every_question_sharing_the_asset(self) -> None:
        """Normalize shared speech provenance instead of copying hashes onto questions; e.g. stochastic re-synthesis cannot invalidate a sibling question."""

        self.opts = self.opts.model_copy(update={"count": 2})
        self.engine.start(self.opts, lambda _: None, background=True)
        policy = {"model": self.provider.model, "reasoning": self.provider.reasoning, "prompt_version": self.engine.start.prompt_version}
        rows = self.db.practice_stock(practice_tracks.Track.LISTENING, self.opts, policy)
        first = self.db.listening_audio(rows[0]["id"])
        paths = self.db.listening_paths(first["cache_key"])
        # Real neural synthesis may produce different valid PCM bytes on a later render.
        paths["normal"].write_bytes(paths["normal"].read_bytes()[:-2] + b"\x01\x01")
        self.db.listening_store(rows[0]["id"], first["cache_key"], first["voice_profile"], paths)
        other = self.db.listening_audio(rows[1]["id"])
        self.assertTrue(self.db.listening_audio_valid(other))
        self.assertNotEqual(first["normal_sha256"], other["normal_sha256"])
        with self.db._connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM listening_assets").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM listening_audio").fetchone()[0], 2)
