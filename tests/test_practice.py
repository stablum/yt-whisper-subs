"""Verify adaptive scheduling, durable rounds, semantic gates, and trustworthy analytics.

Example: `python -m unittest tests.test_practice` uses real SQLite and FSRS with no API calls.
"""

from __future__ import annotations

import json
import argparse
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests import practice_fixtures
from yt_whisper_subs import library_db
from yt_whisper_subs import practice
from yt_whisper_subs import practice_generate
from yt_whisper_subs import practice_service
from yt_whisper_subs import practice_stats


class PracticeTests(unittest.TestCase):
    """Exercise real relational state transitions rather than mirroring internal helpers."""

    def setUp(self) -> None:
        """Create an isolated translated vocabulary; e.g. never read or modify the learner's progress."""

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = library_db.LibraryDb(Path(tmp.name) / "catalog.sqlite3")
        self.db.initialize()
        practice_fixtures.seed(self.db)
        self.generator = practice_fixtures.FakeGenerator()
        self.service = practice_service.PracticeService(self.db, self.generator)

    def _round(self, direction=practice.Direction.NL_EN, count=4) -> str:
        """Prepare distinct terms through the actual service; e.g. include early words for repeat-round tests."""

        opts = practice.RoundOptions(direction=direction, count=count, include_early=True)
        return self.service.start(opts, lambda _: None)

    def test_only_displayed_questions_count_and_resume_is_idempotent(self) -> None:
        """Planning is not exposure; e.g. a crash before opening a question cannot increase its shown count."""

        session = self._round()
        self.assertEqual(self.db.practice_summary(session)["shown"], 0)
        first = self.db.practice_current(session)
        again = self.db.practice_current(session)
        self.assertEqual((first["id"], first["shown_at"]), (again["id"], again["shown_at"]))
        self.assertEqual(self.db.practice_active(), session)
        self.assertEqual(practice_stats.snapshot(self.db)["totals"]["shown"], 1)
        result = self.db.practice_finish(session)
        self.assertEqual((result["shown"], result["wrong"]), (1, 0))

    def test_answers_update_counts_and_schedule_once(self) -> None:
        """Commit an answer and scheduler state atomically; e.g. a double activation never adds a second success."""

        session = self._round()
        first = self.db.practice_current(session)
        result = self.db.practice_answer(first["id"], first["correct_idx"], False, 1250)
        replay = self.db.practice_answer(first["id"], (first["correct_idx"] + 1) % 3, True, 9999)
        self.assertEqual(result, replay)
        self.assertEqual(result["rating"], 3)
        self.assertEqual(json.loads(result["log_json"])["card_id"], first["word_id"] * 2)
        self.assertGreater(result["due_at"], result["answered_at"])
        second = self.db.practice_current(session)
        self.db.practice_answer(second["id"], (second["correct_idx"] + 1) % 3, False, 2750)
        data = practice_stats.snapshot(self.db)
        self.assertEqual((data["totals"]["correct"], data["totals"]["wrong"]), (1, 1))
        self.assertEqual(data["totals"]["accuracy"], 0.5)
        self.assertEqual(data["totals"]["median_ms"], 2000)
        self.assertEqual(sum(row["attempts"] for row in data["daily"]), 2)
        with self.db._connect() as conn:
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")

    def test_directions_have_independent_histories_and_reverse_blanks_answer(self) -> None:
        """A Dutch recognition success does not imply Dutch recall; e.g. reverse practice starts untrained."""

        session = self._round(count=1)
        first = self.db.practice_current(session)
        self.db.practice_answer(first["id"], first["correct_idx"], True, 900)
        self.assertEqual(sum(bool(row["card_json"]) for row in self.db.practice_candidates("nl_en")), 1)
        self.assertTrue(all(row["card_json"] is None for row in self.db.practice_candidates("en_nl")))
        reverse = self._round(practice.Direction.EN_NL)
        row = self.db.practice_current(reverse)
        self.assertEqual(row["content"]["prompt"], row["content"]["meaning"])
        self.assertEqual(row["choices"][row["correct_idx"]]["text"], row["content"]["surface"])
        self.assertNotIn(row["content"]["surface"].casefold(), row["content"]["context"].casefold())
        self.assertIn("____", row["content"]["context"])

    def test_flagging_removes_grading_and_rebuilds_subsequent_schedule(self) -> None:
        """A faulty question cannot poison future spacing; e.g. report an old answer after a later valid review."""

        session = self._round(count=1)
        first = self.db.practice_current(session)
        self.db.practice_answer(first["id"], first["correct_idx"], False, 100)
        self.db.practice_finish(session)
        second_session = self._round(count=4)
        later = None
        while row := self.db.practice_current(second_session):
            result = self.db.practice_answer(row["id"], row["correct_idx"], False, 500)
            if row["word_id"] == first["word_id"]:
                later = result
        self.assertIsNotNone(later)
        self.db.practice_flag(first["id"])
        data = practice_stats.snapshot(self.db)
        self.assertEqual(data["totals"]["attempts"], 4)
        self.assertEqual(data["totals"]["invalid"], 1)
        state = next(row for row in self.db.practice_candidates("nl_en") if row["word_id"] == first["word_id"])
        expected = practice.ReviewPolicy(later["scheduler_json"]).answer(
            None, correct=True, unsure=False, now=later["answered_at"], elapsed_ms=500)
        actual_card, expected_card = json.loads(state["card_json"]), json.loads(expected["after_json"])
        self.assertAlmostEqual(actual_card["stability"], expected_card["stability"])
        self.assertEqual(state["due_at"], expected["due_at"])
        policy = {"model": self.generator.model, "reasoning": self.generator.reasoning,
                  "prompt_version": practice_generate.PROMPT_VERSION}
        self.assertNotIn(first["question_id"], [q["id"] for q in self.db.practice_cached(first["lookup_id"], "nl_en", policy)])

    def test_due_selection_excludes_boring_early_words_and_reserves_new_slots(self) -> None:
        """Spacing controls exposure rather than lifetime failure counts; e.g. well-learned terms wait until due."""

        policy = practice.ReviewPolicy()
        now = time.time()
        rows = []
        for idx in range(8):
            result = policy.answer(None, correct=False, unsure=False, now=now - 1000, elapsed_ms=0)
            rows.append({"word_id": idx, "card_json": result["after_json"], "due_at": result["due_at"]})
        rows += [{"word_id": 100, "card_json": None, "due_at": None}]
        early = policy.answer(None, correct=True, unsure=False, now=now, elapsed_ms=0)
        rows += [{"word_id": 200, "card_json": early["after_json"], "due_at": early["due_at"]}]
        opts = practice.RoundOptions(direction=practice.Direction.NL_EN, count=5)
        selected = policy.select(rows, opts, now)
        self.assertEqual(len({row["word_id"] for row in selected}), 5)
        self.assertIn(100, [row["word_id"] for row in selected])
        self.assertNotIn(200, [row["word_id"] for row in selected])

    def test_lapses_shorten_intervals_and_uncertain_success_is_conservative(self) -> None:
        """Use actual FSRS state changes; e.g. a mistake on an established word returns sooner."""

        policy = practice.ReviewPolicy()
        now, card = time.time(), None
        for _ in range(8):
            review = policy.answer(card, correct=True, unsure=False, now=now, elapsed_ms=4000)
            card, now = review["after_json"], review["due_at"]
        wrong = policy.answer(card, correct=False, unsure=False, now=now, elapsed_ms=4000)
        right = policy.answer(card, correct=True, unsure=False, now=now, elapsed_ms=4000)
        unsure = policy.answer(card, correct=True, unsure=True, now=now, elapsed_ms=4000)
        self.assertLess(wrong["due_at"], right["due_at"])
        self.assertLessEqual(unsure["due_at"], right["due_at"])
        self.assertEqual(unsure["rating"], 2)

    def test_cached_variants_work_offline_and_rejected_questions_never_enter_round(self) -> None:
        """Preserve playable approved content during outages; e.g. no fallback to unchecked or random answers."""

        candidates = self.db.practice_candidates("nl_en")
        self.generator.rejected.add(candidates[0]["lookup_id"])
        session = self._round()
        self.assertEqual(self.db.practice_summary(session)["planned"], 3)
        while row := self.db.practice_current(session):
            self.db.practice_answer(row["id"], row["correct_idx"], False, 100)
        self.db.practice_finish(session)
        self.generator.available = False
        # Remove the rejected, never-cached word from this test's eligibility.
        with self.db._connect() as conn:
            conn.execute("UPDATE vocab_lookups SET ambiguous=1 WHERE id=?", (candidates[0]["lookup_id"],))
        again = self._round(count=3)
        self.assertEqual(self.db.practice_summary(again)["planned"], 3)

    @mock.patch("yt_whisper_subs.openai_client.responses_api_request")
    def test_generator_uses_two_strict_passes_and_rejects_duplicate_choices(self, request) -> None:
        """Verify the real provider contract, including independent semantics review and literal saved answers."""

        rows = self.db.practice_candidates("nl_en")[:1]
        draft = self.generator.generate(rows, practice.Direction.NL_EN)[0]
        request.side_effect = [{"output_text": json.dumps({"questions": [draft["draft"]]})},
                               {"output_text": json.dumps({"verdicts": [draft["verdict"]]})}]
        args = argparse.Namespace(openai_translation_model="subtitle-model", openai_reasoning_effort="high")
        actual = practice_generate.QuestionGenerator(args).generate(rows, practice.Direction.NL_EN)
        self.assertTrue(actual[0]["verdict"]["valid"])
        self.assertEqual(request.call_count, 2)
        for call in request.call_args_list:
            self.assertEqual(call.args[1]["model"], "gpt-6-luna")
            self.assertEqual(call.args[1]["reasoning"], {"effort": "medium"})
            self.assertTrue(call.args[1]["text"]["format"]["strict"])
            self.assertFalse(call.args[1]["store"])
            self.assertNotIn("video.mkv", call.args[1]["input"])
        draft["draft"]["distractors"][0]["text"] = rows[0]["meaning"].upper()
        request.side_effect = [{"output_text": json.dumps({"questions": [draft["draft"]]})}]
        with self.assertRaises(ValueError):
            practice_generate.QuestionGenerator().generate(rows, practice.Direction.NL_EN)

    def test_unseen_questions_cannot_be_answered_and_stopped_round_is_not_graded(self) -> None:
        """Reject invalid lifecycle transitions; e.g. an ended round cannot accept a delayed button event."""

        session = self._round()
        with self.db._connect() as conn:
            row_id = conn.execute("SELECT id FROM practice_presentations WHERE session_id=?", (session,)).fetchone()[0]
        with self.assertRaises(ValueError):
            self.db.practice_answer(row_id, 0, False, 10)
        shown = self.db.practice_current(session)
        self.db.practice_finish(session)
        with self.assertRaises(ValueError):
            self.db.practice_answer(shown["id"], 0, False, 10)

    def test_resumed_answer_is_attributed_to_answer_day_and_coverage_carries_forward(self) -> None:
        """Midnight or a restart must not misdate learning; e.g. yesterday's displayed card is answered today."""

        session = self._round(count=1)
        row = self.db.practice_current(session)
        with self.db._connect() as conn:
            conn.execute("UPDATE practice_presentations SET shown_at=shown_at-172800 WHERE id=?", (row["id"],))
        self.db.practice_answer(row["id"], row["correct_idx"], False, 120)
        data = practice_stats.snapshot(self.db)
        self.assertEqual(len(data["coverage"]), 3)
        self.assertTrue(all(point["words"] == 1 for point in data["coverage"]))
        graded_day = next(point for point in data["daily"] if point["attempts"])
        self.assertEqual(graded_day["day"], data["coverage"][-1]["day"])

    def test_uncached_generation_error_leaves_no_partial_round(self) -> None:
        """A failed provider cannot create phantom study activity; e.g. fix credentials and retry later."""

        self.generator.available = False
        with self.assertRaises(ConnectionError):
            self._round()
        self.assertIsNone(self.db.practice_active())
        self.assertEqual(practice_stats.snapshot(self.db)["totals"]["shown"], 0)

    def test_model_and_reasoning_changes_get_fresh_content_without_losing_history(self) -> None:
        """Cached choices must reflect the requested policy; e.g. upgrading from Mini to Luna never reuses unseen Mini drafts."""

        self.generator.model, self.generator.reasoning = "gpt-5-mini", "low"
        session = self._round()
        shown = self.db.practice_current(session)
        self.db.practice_answer(shown["id"], shown["correct_idx"], False, 700)
        for model, effort in (("gpt-6-luna", "medium"), ("gpt-6-luna", "high")):
            self.generator.model, self.generator.reasoning = model, effort
            fresh = self._round()
            with self.db._connect() as conn:
                policies = conn.execute("SELECT DISTINCT q.model,q.reasoning FROM practice_presentations p "
                                        "JOIN practice_questions q ON q.id=p.question_id WHERE p.session_id=?", (fresh,)).fetchall()
            self.assertEqual([tuple(row) for row in policies], [(model, effort)])
        data = practice_stats.snapshot(self.db)
        self.assertEqual((data["totals"]["shown"], data["totals"]["correct"]), (1, 1))
        with self.db._connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM practice_questions").fetchone()[0], 12)

    def test_new_model_cannot_fall_back_to_old_model_cache_when_offline(self) -> None:
        """An unavailable requested model must fail visibly; e.g. preserve an old active round instead of silently downgrading."""

        self.generator.model, self.generator.reasoning = "gpt-5-mini", "low"
        original = self._round()
        shown = self.db.practice_current(original)
        self.generator.model, self.generator.reasoning = "gpt-6-luna", "medium"
        self.generator.available = False
        with self.assertRaises(ConnectionError):
            self._round()
        self.assertEqual(self.db.practice_active(), original)
        self.assertEqual(self.db.practice_current(original)["choices"], shown["choices"])
