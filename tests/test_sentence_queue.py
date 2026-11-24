"""Exercise durable correction ownership and learning order without API calls.

Example: an older correction retried after a newer answer produces the same skill states as chronological grading.
"""

import contextlib
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import psutil

from tests import sentence_fixtures as fixtures
from yt_whisper_subs import correction_queue
from yt_whisper_subs import practice_tracks
from yt_whisper_subs import library_db
from yt_whisper_subs import practice
from yt_whisper_subs import practice_activity
from yt_whisper_subs import sentence_service


class SentenceQueueTests(unittest.TestCase):
    """Use real submissions, leases and grade transactions; e.g. queue state never substitutes for actual learning evidence."""

    def setUp(self):
        """Create isolated catalog storage; e.g. workers cannot touch a personal practice history."""

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.db = library_db.LibraryDb(self.folder / "catalog.sqlite3")
        self.db.initialize()
        fixtures.seed(self.db)
        self.provider = fixtures.FakeSentenceProvider()
        self.engine = sentence_service.SentenceService(self.db, self.provider)
        self.opts = fixtures.options().model_copy(update={"include_early": True})
        self.session = self.engine.start(self.opts, lambda _: None)
        self.row = self.db.sentence_current(self.session)
        self.owner = f"{os.getpid()}:{psutil.Process().create_time()}:test-owner"

    def test_submit_and_job_are_atomic_and_idempotent(self):
        """Save one immutable attempt and one queued job together; e.g. a repeated Enter cannot replace its text or duplicate activity."""

        first = self.db.sentence_submit(self.row["id"], fixtures.DUTCH, 1000)
        again = self.db.sentence_submit(self.row["id"], "Another attempt", 2000)
        self.assertEqual(again["answer_text"], first["answer_text"])
        self.assertEqual(practice_activity.read(self.db).answers, 1)
        self.assertEqual(self.db.written_history("sentence", self.session)[0]["state"], "queued")
        with self.db._connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sentence_checks").fetchone()[0], 1)

    def test_lease_prevents_duplicate_work_and_recovers_dead_owner(self):
        """Retain ownership across dialogs and reclaim crashes; e.g. a live request cannot be submitted twice."""

        self.db.sentence_submit(self.row["id"], fixtures.DUTCH, 1000)
        self.assertEqual(correction_queue.Queue(self.db).claim(self.owner)["id"], self.row["id"])
        self.assertIsNone(correction_queue.Queue(self.db).claim(self.owner + "-other"))
        with self.db._connect() as conn:
            conn.execute("UPDATE sentence_checks SET owner='99999999:0:dead'")
        self.assertEqual(correction_queue.Queue(self.db).claim(self.owner)["id"], self.row["id"])
        with self.db._connect() as conn:
            self.assertEqual(conn.execute("SELECT attempts FROM sentence_checks").fetchone()[0], 2)

    def test_failures_wait_for_explicit_retry_without_false_failures(self):
        """Avoid automatic paid retry loops; e.g. a network outage retains the first answer but adds no wrong learning result."""

        self.db.sentence_submit(self.row["id"], fixtures.DUTCH, 1000)
        correction_queue.Queue(self.db).claim(self.owner)
        correction_queue.Queue(self.db).done(self.row["id"], self.owner, error="Provider unavailable")
        self.assertIsNone(correction_queue.Queue(self.db).claim(self.owner))
        self.assertIsNone(self.db.sentence_get(self.row["id"])["outcome"])
        correction_queue.Queue(self.db).retry(self.row["id"])
        self.assertEqual(correction_queue.Queue(self.db).claim(self.owner)["id"], self.row["id"])
        self.assertEqual(practice_activity.read(self.db).answers, 1)

    def test_stopped_round_still_accepts_submitted_correction(self):
        """Finish typing independently of queued work; e.g. a previous round contributes its correction after a new one starts."""

        self.db.sentence_submit(self.row["id"], fixtures.DUTCH, 1000)
        self.db.sentence_finish(self.session)
        first = self.engine.check(self.row["id"], lambda _: None)
        again = self.engine.check(self.row["id"], lambda _: None)
        self.assertEqual((first["outcome"], again["outcome"]), ("correct", "correct"))
        self.assertEqual(self.db.sentence_summary(self.session)["correct"], 1)
        self.assertEqual(practice_activity.read(self.db).answers, 1)

    def test_out_of_order_corrections_replay_submission_order(self):
        """Keep analytical states independent of response order; e.g. an older retry cannot replace the latest successful review date."""

        now = time.time()
        with mock.patch("yt_whisper_subs.sentence_db.time.time", return_value=now - 86400):
            self.db.sentence_submit(self.row["id"], fixtures.DUTCH.replace("slimste", "slimmere"), 1000)
        following = self.db.sentence_create(self.opts, [{"id": self.row["question_id"]}], practice.ReviewPolicy().document())
        second = self.db.sentence_current(following)
        with mock.patch("yt_whisper_subs.sentence_db.time.time", return_value=now):
            self.db.sentence_submit(second["id"], fixtures.DUTCH, 1000)
        reference = library_db.LibraryDb(self.folder / "chronological.sqlite3")
        with contextlib.closing(sqlite3.connect(self.db.path)) as source, contextlib.closing(sqlite3.connect(reference.path)) as target:
            source.backup(target)
        chronological = sentence_service.SentenceService(reference, fixtures.FakeSentenceProvider())
        for presentation_id in (self.row["id"], second["id"]):
            chronological.check(presentation_id, lambda _: None)
        for presentation_id in (second["id"], self.row["id"]):
            self.engine.check(presentation_id, lambda _: None)
        for table in ("practice_states", "sentence_states", "sentence_question_states"):
            with self.subTest(table=table), self.db._connect() as actual, reference._connect() as expected:
                rows = [dict(row) for row in actual.execute(f"SELECT * FROM {table} ORDER BY 1,2")]
                other = [dict(row) for row in expected.execute(f"SELECT * FROM {table} ORDER BY 1,2")]
                self.assertEqual(rows, other)
        self.assertEqual(practice_activity.read(self.db).answers, 2)

    def test_drafts_do_not_count_as_activity_and_cannot_overwrite_submission(self):
        """Persist navigation drafts separately from attempts; e.g. a late autosave cannot alter a submitted sentence."""

        self.db.written_save_draft("sentence", self.row["id"], "Mijn concept", 3000)
        self.assertEqual(self.db.written_draft("sentence", self.row["id"])["text"], "Mijn concept")
        self.assertEqual(practice_activity.read(self.db).answers, 0)
        self.db.sentence_submit(self.row["id"], fixtures.DUTCH, 4000)
        self.db.written_save_draft("sentence", self.row["id"], "A stale draft", 5000)
        self.assertEqual(self.db.sentence_get(self.row["id"])["answer_text"], fixtures.DUTCH)
        self.assertEqual(self.db.written_draft("sentence", self.row["id"])["text"], "")

    def test_feedback_consultation_is_durable_idempotent_and_not_practice(self):
        """Track actual feedback views separately from submissions; e.g. a pending answer cannot be marked consulted."""

        self.db.sentence_submit(self.row["id"], fixtures.DUTCH, 1000)
        self.assertIsNone(self.db.written_seen("sentence", self.row["id"]))
        self.engine.check(self.row["id"], lambda _: None)
        self.assertIsNone(self.db.sentence_get(self.row["id"])["viewed_at"])
        self.assertEqual(self.db.written_unseen_session(practice_tracks.Track.SENTENCE_EN_NL), self.session)
        viewed_at = self.db.written_seen("sentence", self.row["id"])
        self.assertIsNotNone(viewed_at)
        with mock.patch("yt_whisper_subs.written_review_db.time.time", return_value=time.time() + 3600):
            self.assertEqual(self.db.written_seen("sentence", self.row["id"]), viewed_at)
        self.db.initialize()
        self.assertEqual(self.db.written_history("sentence", self.session)[0]["viewed_at"], viewed_at)
        self.assertIsNone(self.db.written_unseen_session(practice_tracks.Track.SENTENCE_EN_NL))
        self.assertEqual(practice_activity.read(self.db).answers, 1)
