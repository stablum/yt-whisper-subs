"""Exercise relational verb history, diagnostic grading and error-aware scheduling.

Example: `python -m unittest tests.test_verbs` uses real SQLite and FSRS with an offline provider.
"""

import tempfile
import json
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from tests import verb_fixtures
from yt_whisper_subs import library_db
from yt_whisper_subs import practice
from yt_whisper_subs import practice_activity
from yt_whisper_subs import practice_service
from yt_whisper_subs import verb_practice as verbs
from yt_whisper_subs import verb_generate
from yt_whisper_subs import verb_service
from yt_whisper_subs import verb_stats
from yt_whisper_subs import verb_syllabus
from yt_whisper_subs import task_cancel


class VerbTests(unittest.TestCase):
    """Check actual durable behavior; e.g. uncertain corrections cannot become adaptive failure evidence."""

    def setUp(self) -> None:
        """Create a fresh catalog with no captured vocabulary; e.g. verb practice stands on its own."""

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = library_db.LibraryDb(Path(tmp.name) / "catalog.sqlite3")
        self.db.initialize()
        self.catalog = verb_fixtures.catalog(self.db)
        patch = mock.patch.object(self.db, "verb_catalog", return_value=self.catalog)
        patch.start()
        self.addCleanup(patch.stop)
        self.provider = verb_fixtures.FakeVerbProvider()
        self.engine = verb_service.VerbService(self.db, self.provider)

    def _round(self, count=2) -> str:
        """Prepare a genuine local round; e.g. include early targets when testing repeated exposure."""

        opts = verb_fixtures.options(count).model_copy(update={"include_early": True})
        return self.engine.start(opts, lambda _: None)

    def _submit(self, row: dict, *, wrong: bool = False) -> dict:
        """Save a real first attempt; e.g. removing the jij ending creates a specific agreement error."""

        answer = row["content"]["answer"]
        if wrong:
            answer = answer.replace("werkt", "werk").replace("maakt", "maak")
        return self.db.verb_submit(row["id"], answer, 2200)

    def test_round_exposure_and_first_submission_are_durable_idempotent(self) -> None:
        """Planning is not presentation or practice; e.g. duplicate submits preserve the exact first sentence."""

        session = self._round()
        self.assertEqual(self.db.verb_summary(session)["shown"], 0)
        self.assertEqual(practice_activity.read(self.db).answers, 0)
        row = self.db.verb_current(session)
        self.assertEqual(row, self.db.verb_current(session))
        saved = self._submit(row)
        duplicate = self.db.verb_submit(row["id"], "A different second answer", 9999)
        self.assertEqual(saved, duplicate)
        self.assertIsNone(saved["outcome"])
        self.assertEqual(practice_activity.read(self.db).answers, 1)
        self.assertIsNotNone(self.db.practice_last_answer(time.time() + 1))
        resumed = library_db.LibraryDb(self.db.path).verb_current(session)
        self.assertEqual(resumed["answer_text"], saved["answer_text"])

    def test_exact_approved_answers_and_alternatives_need_no_remote_grade(self) -> None:
        """Ignore harmless case and punctuation only; e.g. matching a checked variant stays usable offline."""

        session = self._round()
        row = self.db.verb_current(session)
        answer = row["content"]["alternatives"][0].upper().replace(".", "!")
        self.db.verb_submit(row["id"], answer, 20)
        self.provider.available = False
        graded = self.engine.check(row["id"], lambda _: None)
        repeated = self.engine.check(row["id"], lambda _: None)
        self.assertEqual(graded, repeated)
        self.assertEqual((graded["outcome"], graded["rating"], self.provider.graded), ("correct", 3, 0))
        self.assertEqual(graded["grader_model"], "approved_exact_match")

    def test_wrong_answer_records_specific_error_not_all_axes(self) -> None:
        """Diagnose agreement independently; e.g. a missing ending need not count against the requested tense."""

        session = self._round()
        row = self.db.verb_current(session)
        self._submit(row, wrong=True)
        graded = self.engine.check(row["id"], lambda _: None)
        self.assertEqual((graded["outcome"], graded["rating"]), ("wrong", 1))
        stats = verb_stats.snapshot(self.db)
        metrics = {item["axis"]: item for item in stats["metrics"]}
        self.assertEqual(metrics["subject"]["failures"], 1)
        self.assertEqual(metrics["tense"]["failures"], 0)
        self.assertEqual(stats["kinds"], [{"kind": "agreement", "answers": 1, "errors": 1}])
        with self.db._connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM verb_assessments").fetchone()[0], 4)
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_failed_or_uncertain_grading_preserves_pending_attempt_without_failure(self) -> None:
        """Network and confidence failures do not fabricate wrong answers; e.g. retry checking after restarting."""

        row = self.db.verb_current(self._round())
        saved = self._submit(row, wrong=True)
        self.provider.available = False
        with self.assertRaises(OSError):
            self.engine.check(row["id"], lambda _: None)
        stats = verb_stats.snapshot(self.db)
        self.assertEqual((stats["submitted"], stats["pending"], stats["wrong"]), (1, 1, 0))
        self.provider.available = True
        draft = self.provider.grade({"target": row, "exercise": row["content"]})
        self.provider.grade_result = draft.model_copy(update={"confident": False})
        with self.assertRaisesRegex(ValueError, "uncertain"):
            self.engine.check(row["id"], lambda _: None)
        self.assertEqual(self.db.verb_get(row["id"])["answered_at"], saved["answered_at"])
        self.provider.grade_result = None
        self.engine.check(row["id"], lambda _: None)
        self.assertEqual(practice_activity.read(self.db).answers, 1)

    def test_report_excludes_all_uses_and_rebuilds_the_affected_schedule(self) -> None:
        """Keep effort but discard faulty linguistic evidence; e.g. repeated cached questions cannot keep an invalid failure."""

        row = self.db.verb_current(self._round(1))
        self._submit(row, wrong=True)
        self.engine.check(row["id"], lambda _: None)
        original = self.db.verb_get(row["id"])
        self.db.verb_flag(row["id"])
        self.assertEqual(self.db.verb_get(row["id"])["answer_text"], original["answer_text"])
        self.assertEqual(verb_stats.metrics(self.db), [])
        self.assertEqual(verb_stats.snapshot(self.db)["wrong"], 0)
        self.assertEqual(practice_activity.read(self.db).answers, 1)
        with self.db._connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM verb_states").fetchone()[0], 0)
        self.assertIsNone(self.db.verb_active())

    def test_unanswered_report_does_not_count_as_activity(self) -> None:
        """Reporting an exposed defective exercise is not a typed attempt; e.g. the daily reminder stays due."""

        row = self.db.verb_current(self._round(1))
        self.db.verb_flag(row["id"])
        self.assertEqual(practice_activity.read(self.db).answers, 0)
        self.assertIsNone(self.db.practice_last_answer(time.time() + 1))

    def test_generation_failure_or_rejection_preserves_active_round(self) -> None:
        """Only approved complete preparation can replace a round; e.g. a new-policy outage leaves old questions resumable."""

        session = self._round()
        self.provider.model = "new-policy"
        self.provider.available = False
        with self.assertRaises(OSError):
            self._round()
        self.assertEqual(self.db.verb_active(), session)
        self.provider.available = True
        self.provider.approved = False
        with self.assertRaises(practice_service.NothingToPractice):
            self._round()
        self.assertEqual(self.db.verb_active(), session)

    def test_rejected_targets_receive_one_bounded_repair(self) -> None:
        """Recover a failed quality check without looping indefinitely; e.g. pass the original critique to one replacement batch."""

        original = self.provider.generate
        calls = []

        def repair(targets):
            """Reject only the first batch; e.g. emulate a useful independent quality gate."""

            calls.append(targets)
            self.provider.approved = len(calls) > 1
            return original(targets)

        self.provider.generate = repair
        session = self._round()
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(row.get("previous_rejection") for row in calls[1]))
        self.assertEqual(self.db.verb_summary(session)["planned"], 2)

    def test_cancelled_http_result_cannot_proceed_to_audit_or_cache(self) -> None:
        """Observe cooperative cancellation after HTTP; e.g. closing during a request cannot commit a delayed correction."""

        control = task_cancel.TaskControl()

        def cancel(*_args):
            """Cancel at the network boundary; e.g. simulate a user closing while the provider responds."""

            control.cancel()
            return {"output_text": "{}"}

        with mock.patch("yt_whisper_subs.openai_client.responses_api_request", side_effect=cancel) as request:
            with task_cancel.activate(control), self.assertRaises(task_cancel.CancelledError):
                verb_generate.VerbGenerator().generate([])
            self.assertEqual(request.call_count, 1)

    def test_successful_targets_wait_until_due_and_cached_rounds_work_offline(self) -> None:
        """Back off established combinations; e.g. explicit early practice can reuse checked questions without HTTP."""

        session = self._round()
        while row := self.db.verb_current(session):
            self._submit(row)
            self.engine.check(row["id"], lambda _: None)
        self.provider.available = False
        with self.assertRaises(practice_service.NothingToPractice) as empty:
            self.engine.start(verb_fixtures.options(), lambda _: None)
        self.assertEqual(empty.exception.reason, "not_due")
        calls = self.provider.generated
        self._round()
        self.assertEqual(self.provider.generated, calls)

    def test_all_forms_include_conditionals_and_only_real_subjunctive_targets(self) -> None:
        """Constrain rare moods grammatically; e.g. there is no invented past subjunctive of lopen."""

        catalog = [{"id": idx, "lemma": lemma, "family": family, "transitive": transitive,
                    "starter": True, "lookups": 0, "clicks": 0, "videos": 0}
                   for idx, (lemma, family, transitive) in enumerate(verb_syllabus.CATALOG, 1)]
        rows = verb_syllabus.candidates(catalog, [], verbs.Options())
        self.assertEqual({row["tense"] for row in rows}, {tense.value for tense in verbs.Tense})
        rare = [row for row in rows if row["tense"] == "subjunctive_past"]
        self.assertEqual([(row["lemma"], row["subject"], row["structure"]) for row in rare], [("zijn", "het", "formal")])
        conditional = verb_syllabus.candidates(catalog, [], verbs.Options(focus="conditionals"))
        self.assertEqual({row["tense"] for row in conditional}, {"conditional", "conditional_perfect"})
        self.assertTrue(any(row["structure"] == "if_clause" for row in conditional))
        self.assertFalse(any(row["structure"] == "passive" and not row["transitive"] for row in rows))
        weights = verb_syllabus.Selector(practice.ReviewPolicy()).weights(rows, [], time.time())
        present = sum(w for row, w in zip(rows, weights) if row["tense"] == "present")
        subjunctive = sum(w for row, w in zip(rows, weights) if row["tense"] == "subjunctive")
        self.assertAlmostEqual(subjunctive / present, 0.18)

    def test_weak_dimensions_increase_selection_weight_and_sampling_is_unique(self) -> None:
        """Verify error generalization without flaky random thresholds; e.g. tense mistakes boost unseen verbs in that tense."""

        rows = verb_syllabus.candidates(self.catalog, [], verbs.Options(focus="everyday"))
        selector = verb_syllabus.Selector(practice.ReviewPolicy(), np.random.default_rng(12))
        base = selector.weights(rows, [], time.time())
        metrics = [{"axis": "tense", "value": "past", "attempts": 20, "failures": 20}]
        boosted = selector.weights(rows, metrics, time.time())
        mask = np.array([row["tense"] == "past" for row in rows])
        self.assertTrue(np.all(boosted[mask] > base[mask]))
        self.assertTrue(np.all(boosted[~mask] == base[~mask]))
        chosen = selector.select(rows, metrics, verbs.Options(count=50), time.time())
        keys = [(row["verb_id"], row["tense"], row["subject"], row["structure"]) for row in chosen]
        self.assertEqual(len(keys), len(set(keys)))

    def test_contradictory_or_fabricated_grade_is_rejected(self) -> None:
        """Do not store ungrounded diagnostic evidence; e.g. the grader cannot quote a word the learner never typed."""

        row = self.db.verb_current(self._round())
        self._submit(row, wrong=True)
        grade = self.provider.grade({"target": row, "exercise": row["content"]})
        grade.errors[0].evidence = "not in the submitted answer"
        with self.assertRaisesRegex(ValueError, "absent"):
            self.db.verb_grade(row["id"], grade, {"model": "test", "reasoning": "low", "grade_version": 1})
        self.assertEqual(verb_stats.snapshot(self.db)["graded"], 0)

    @mock.patch("yt_whisper_subs.openai_client.responses_api_request")
    def test_provider_uses_separate_strict_generation_audit_and_grading(self, request) -> None:
        """Verify the actual Responses contracts; e.g. every linguistic call uses the configured Luna policy and store=false."""

        row = self.db.verb_current(self._round())
        target = {key: row[key] for key in ("target_id", "lemma", "family", "transitive", "tense", "subject", "structure")}
        item = self.provider.generate([target])[0]
        grade = self.provider.grade({"target": row, "exercise": row["content"]})
        request.side_effect = [{"output_text": json.dumps({"questions": [item["draft"]]})},
                               {"output_text": json.dumps({"verdicts": [item["verdict"]]})},
                               {"output_text": grade.model_dump_json()}]
        provider = verb_generate.VerbGenerator()
        result = provider.generate([target])
        checked = provider.grade({"target": target, "exercise": row["content"], "learner_answer": "Jij werk vandaag."})
        self.assertTrue(result[0]["verdict"]["valid"])
        self.assertFalse(checked.correct)
        self.assertEqual(request.call_count, 3)
        for call in request.call_args_list:
            payload = call.args[1]
            self.assertEqual(payload["model"], "gpt-6-luna")
            self.assertEqual(payload["reasoning"], {"effort": "medium"})
            self.assertTrue(payload["text"]["format"]["strict"])
            self.assertFalse(payload["store"])

    def test_reporting_one_variant_replays_remaining_valid_review(self) -> None:
        """Rebuild rather than erase unrelated progress; e.g. another checked variant of the same target remains valid."""

        first = self.db.verb_current(self._round(1))
        self._submit(first, wrong=True)
        self.engine.check(first["id"], lambda _: None)
        draft = verbs.Draft.model_validate(first["content"])
        draft.source = draft.source.replace("gisteren", "vorige week")
        verdict = verbs.Verdict(target_id=first["target_id"], valid=True, reason="Second validated variant")
        question = self.db.verb_cache(draft, verdict, {"model": "test", "reasoning": "low", "prompt_version": 1})
        policy = practice.ReviewPolicy()
        session = self.db.verb_create(verb_fixtures.options(1), [question], policy.document())
        second = self.db.verb_current(session)
        saved = self._submit(second)
        self.engine.check(second["id"], lambda _: None)
        self.db.verb_flag(first["id"])
        expected = policy.answer(practice.ReviewPolicy.new_card(first["target_id"], saved["answered_at"]),
                                 correct=True, unsure=False, now=saved["answered_at"], elapsed_ms=2200)
        with self.db._connect() as conn:
            actual = conn.execute("SELECT card_json FROM verb_states WHERE target_id=?", (first["target_id"],)).fetchone()[0]
        self.assertEqual(json.loads(actual), json.loads(expected["after_json"]))
        self.assertEqual(verb_stats.snapshot(self.db)["correct"], 1)


if __name__ == "__main__":
    unittest.main()
