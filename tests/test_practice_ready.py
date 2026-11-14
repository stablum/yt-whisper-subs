"""Verify durable, bounded background stock and foreground generation coordination.

Example: ready questions survive app restarts and count only when actually shown or answered.
"""

import concurrent.futures
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets
from PySide6 import QtTest

from tests import practice_fixtures
from tests import sentence_fixtures
from tests import verb_fixtures
from yt_whisper_subs import practice_round
from yt_whisper_subs import library_db
from yt_whisper_subs import library_practice_ready
from yt_whisper_subs import practice
from yt_whisper_subs import practice_activity
from yt_whisper_subs import practice_service
from yt_whisper_subs import sentence_select
from yt_whisper_subs import sentence_service
from yt_whisper_subs import verb_service


class ReadyTests(unittest.TestCase):
    """Exercise actual SQLite leases and ready rounds; e.g. no API credits or personal history are touched."""

    @classmethod
    def setUpClass(cls) -> None:
        """Provide native controller ownership; e.g. shutdown cancels its timer without touching the generated stock."""

        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def _case(self, kind: str):
        """Create one genuine practice domain; e.g. fake linguistic providers still use production persistence and scheduling."""

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = library_db.LibraryDb(Path(tmp.name) / "catalog.sqlite3")
        db.initialize()
        if kind == "sentence":
            sentence_fixtures.seed(db)
            provider = sentence_fixtures.FakeSentenceProvider()
            engine = sentence_service.SentenceService(db, provider)
            opts = sentence_fixtures.options().model_copy(update={"count": 2, "scene": "mixed"})
        else:
            practice_fixtures.seed(db)
            if kind == "practice":
                provider = practice_fixtures.FakeGenerator()
                engine = practice_service.PracticeService(db, provider)
                opts = practice.RoundOptions(direction=practice.Direction.NL_EN, count=2)
            else:
                catalog = verb_fixtures.catalog(db)
                patch = mock.patch.object(db, "verb_catalog", return_value=catalog)
                patch.start()
                self.addCleanup(patch.stop)
                provider = verb_fixtures.FakeVerbProvider()
                engine = verb_service.VerbService(db, provider)
                opts = verb_fixtures.options().model_copy(update={"source": "starter"})
        db.set_setting(f"{kind}_options", opts.model_dump_json())
        ready = library_practice_ready.PracticeReady(db, modes={kind: (opts, engine)})
        self.addCleanup(self._stop, ready)
        return db, provider, engine, opts, ready

    def _stop(self, ready) -> None:
        """Drain the owned preparation lane before deleting its database; e.g. even a failed native test cannot orphan a writer."""

        ready.stop()
        self.assertTrue(ready._pool.waitForDone(15000))
        self.app.processEvents()

    def test_each_mode_prepares_one_round_without_exposure_and_reuses_it_after_restart(self) -> None:
        """Keep one consumable durable round; e.g. re-entering a mode never replaces an untouched paid batch."""

        for kind in ("practice", "verb", "sentence"):
            with self.subTest(kind=kind):
                db, provider, engine, opts, ready = self._case(kind)
                ready.prepare(lambda _: None)
                session = getattr(db, f"{kind}_active")()
                self.assertIsNotNone(session)
                self.assertEqual(getattr(db, f"{kind}_summary")(session)["shown"], 0)
                self.assertEqual(practice_activity.read(db).answers, 0)
                with db._connect() as conn:
                    before = conn.execute(f"SELECT COUNT(*) FROM {kind}_questions").fetchone()[0]
                provider.available = False
                ready.prepare(lambda _: None)
                reopened = type(engine)(library_db.LibraryDb(db.path), provider)
                self.assertEqual(reopened.start(opts, lambda _: None), session)
                with db._connect() as conn:
                    self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {kind}_questions").fetchone()[0], before)

    def test_partial_round_and_pending_answer_block_refill(self) -> None:
        """Treat unanswered saved work as the stock; e.g. waiting for a correction cannot cause another automatic round."""

        db, provider, engine, opts, ready = self._case("sentence")
        ready.prepare(lambda _: None)
        session = db.sentence_active()
        row = db.sentence_current(session)
        db.sentence_submit(row["id"], row["content"]["dutch"], 1200)
        calls = len(provider.calls)
        with db._connect() as conn:
            conn.execute("UPDATE practice_preparation SET next_at=0")
        ready.prepare(lambda _: None)
        self.assertEqual((db.sentence_active(), len(provider.calls)), (session, calls))
        self.assertEqual(practice_activity.read(db).answers, 1)
        self.assertEqual(db.sentence_get(row["id"])["answer_text"], row["content"]["dutch"])

    def test_stopped_sentence_round_reuses_unseen_pairs_across_random_plans(self) -> None:
        """Do not strand paid sentences behind random context/companion cache keys; e.g. a changed mixed-scene draw needs no new model call."""

        db, provider, engine, opts, _ = self._case("sentence")
        with mock.patch.object(sentence_select.Selector, "_scene", return_value="home"):
            session = engine.start(opts, lambda _: None)
        with db._connect() as conn:
            ids = {row[0] for row in conn.execute("SELECT question_id FROM sentence_presentations WHERE session_id=?", (session,))}
        db.sentence_finish(session)
        provider.available = False
        with mock.patch.object(sentence_select.Selector, "_scene", return_value="travel"):
            following = engine.start(opts, lambda _: None, cached_first=False)
        with db._connect() as conn:
            reused = {row[0] for row in conn.execute("SELECT question_id FROM sentence_presentations WHERE session_id=?", (following,))}
        self.assertEqual(ids, reused)
        self.assertEqual(len(provider.calls), 1)

    def test_foreground_waits_for_same_background_job(self) -> None:
        """Coalesce actual concurrent callers; e.g. opening Sentences halfway through preparation spends tokens once."""

        db, provider, engine, opts, ready = self._case("sentence")
        entered, release, waiting = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        generate = provider.generate

        def delayed(*args):
            """Hold real production coordination at the network boundary; e.g. the foreground must observe the existing lease."""

            entered.set()
            release.wait(5)
            return generate(*args)

        provider.generate = delayed
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            background = pool.submit(ready.prepare, lambda _: None)
            self.assertTrue(entered.wait(5))
            foreground = pool.submit(engine.start, opts, lambda message: waiting.set() if "background" in message else None)
            self.assertTrue(waiting.wait(5))
            release.set()
            background.result(timeout=5)
            session = foreground.result(timeout=5)
        self.assertEqual((session, len(provider.calls)), (db.sentence_active(), 1))
        self.assertEqual(db.sentence_summary(session)["shown"], 0)

    def test_failure_backoff_survives_restart_but_explicit_retry_can_recover(self) -> None:
        """Bound background retries without blocking the learner; e.g. reconnecting and opening practice can retry immediately."""

        db, provider, engine, opts, ready = self._case("sentence")
        provider.available = False
        ready.prepare(lambda _: None)
        status = db.practice_prepare_status("sentence")
        self.assertIn("unavailable", status["error"])
        self.assertGreater(status["next_at"], time.time() + 3500)
        ready.prepare(lambda _: None)
        self.assertEqual(len(provider.calls), 1)
        provider.available = True
        self.assertIsNotNone(engine.start(opts, lambda _: None))
        self.assertEqual(db.practice_prepare_status("sentence")["error"], "")

    def test_stale_lease_recovery_never_unlocks_the_new_owner(self) -> None:
        """Recover abandoned preparations safely; e.g. a late worker cannot release a replacement worker's lease."""

        db, *_ = self._case("sentence")
        now = time.time()
        with mock.patch("yt_whisper_subs.practice_ready.time.time", return_value=now):
            self.assertTrue(db.practice_prepare_claim("sentence", "first"))
            self.assertFalse(db.practice_prepare_claim("sentence", "second"))
        with mock.patch("yt_whisper_subs.practice_ready.time.time", return_value=now + 901):
            self.assertTrue(db.practice_prepare_claim("sentence", "second"))
            db.practice_prepare_done("sentence", "first", "late cancellation")
            self.assertFalse(db.practice_prepare_touch("sentence", "first"))
            self.assertTrue(db.practice_prepare_touch("sentence", "second"))
        self.assertEqual(db.practice_prepare_status("sentence")["owner"], "second")

    def test_crashed_process_lease_is_reclaimed_immediately(self) -> None:
        """Avoid a 15-minute startup stall after a hard exit; e.g. an abandoned owner's missing PID releases its lease immediately."""

        db, *_ = self._case("sentence")
        self.assertTrue(db.practice_prepare_claim("sentence", "987654321:0:abandoned"))
        self.assertTrue(db.practice_prepare_claim("sentence", "replacement"))
        self.assertEqual(db.practice_prepare_status("sentence")["owner"], "replacement")

    def test_unused_questions_from_stopped_rounds_are_consumed_before_new_content_in_every_mode(self) -> None:
        """Prefer already paid unused questions within adaptive eligibility; e.g. shortening a round cannot force avoidable generation."""

        for kind in ("practice", "verb", "sentence"):
            with self.subTest(kind=kind):
                db, provider, engine, opts, _ = self._case(kind)
                session = engine.start(opts, lambda _: None)
                getattr(db, f"{kind}_current")(session)
                with db._connect() as conn:
                    unused = conn.execute(f"SELECT question_id FROM {kind}_presentations WHERE session_id=? AND shown_at IS NULL",
                                          (session,)).fetchone()[0]
                getattr(db, f"{kind}_finish")(session)
                provider.available = False
                following = engine.start(opts.model_copy(update={"count": 1}), lambda _: None, cached_first=False)
                with db._connect() as conn:
                    reused = conn.execute(f"SELECT question_id FROM {kind}_presentations WHERE session_id=?", (following,)).fetchone()[0]
                self.assertEqual(reused, unused)

    def test_native_controller_fills_without_showing_or_answering(self) -> None:
        """Exercise the actual Qt worker and completion signal; e.g. the first ready round exists before any Practice dialog."""

        db, _, _, _, ready = self._case("sentence")
        ready.check()
        deadline = time.monotonic() + 10
        while ready._task is not None and time.monotonic() < deadline:
            QtTest.QTest.qWait(10)
        self.assertIsNone(ready._task)
        self.assertIsNotNone(db.sentence_active())
        self.assertEqual(db.sentence_summary(db.sentence_active())["shown"], 0)
        self.assertEqual(practice_activity.read(db).answers, 0)

    def test_not_due_waits_for_scheduling_without_provider_failure_backoff(self) -> None:
        """Keep minute-scale learning steps available; e.g. all-caught-up is a healthy state rather than a one-hour outage."""

        db, _, engine, opts, _ = self._case("sentence")
        session = engine.start(opts, lambda _: None)
        while row := db.sentence_current(session):
            db.sentence_submit(row["id"], row["content"]["dutch"], 1200)
            engine.check(row["id"], lambda _: None)
        with self.assertRaises(practice_round.NothingToPractice):
            engine.start(opts, lambda _: None)
        self.assertLess(db.practice_prepare_status("sentence")["next_at"], time.time() + 65)

    def test_background_rechecks_existing_round_under_lease_even_when_settings_changed(self) -> None:
        """Protect against a foreground/background ordering race; e.g. a waiting refill cannot replace the learner's newly opened round."""

        db, provider, engine, opts, _ = self._case("sentence")
        session = engine.start(opts, lambda _: None)
        row = db.sentence_current(session)
        provider.available = False
        retained = engine.start(opts.model_copy(update={"count": 1}), lambda _: None, background=True)
        self.assertEqual(retained, session)
        self.assertEqual(db.sentence_current(retained)["id"], row["id"])
        self.assertEqual(len(provider.calls), 1)
