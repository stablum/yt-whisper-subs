"""Verify sentence translation, adaptive word/form learning and transactional evidence.

Example: `python -m unittest tests.test_sentence_practice` uses actual SQLite and FSRS without API calls.
"""

import json
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import numpy as np

from tests import sentence_fixtures as fixtures
from yt_whisper_subs import library_db
from yt_whisper_subs import practice
from yt_whisper_subs import practice_activity
from yt_whisper_subs import practice_service
from yt_whisper_subs import sentence_generate
from yt_whisper_subs import sentence_practice as sentences
from yt_whisper_subs import sentence_select
from yt_whisper_subs import sentence_service
from yt_whisper_subs import sentence_stats


class SentenceTests(unittest.TestCase):
    """Check real learning behavior and failure boundaries; e.g. one grammar mistake cannot erase known vocabulary."""

    def setUp(self) -> None:
        """Keep histories isolated and populate genuine source clicks; e.g. no personal vocabulary is read."""

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = library_db.LibraryDb(Path(tmp.name) / "catalog.sqlite3")
        self.db.initialize()
        fixtures.seed(self.db)
        self.provider = fixtures.FakeSentenceProvider()
        self.engine = sentence_service.SentenceService(self.db, self.provider)

    def _round(self, **changes) -> str:
        """Prepare a fully audited fixture round; e.g. direction changes use the same underlying saved concepts."""

        return self.engine.start(fixtures.options(**changes), lambda _: None)

    def _answer(self, row: dict, answer: str | None = None) -> dict:
        """Submit and check the original attempt; e.g. exact approved answers follow the local fast path."""

        key = "dutch" if row["direction"] == "en_nl" else "english"
        self.db.sentence_submit(row["id"], answer or row["content"][key], 2200)
        return self.engine.check(row["id"], lambda _: None)

    def test_two_directions_have_distinct_word_and_sentence_schedules(self) -> None:
        """Keep reading and writing histories separate; e.g. successful English comprehension cannot imply Dutch production mastery."""

        for direction in (practice.Direction.EN_NL, practice.Direction.NL_EN):
            row = self.db.sentence_current(self._round(direction=direction))
            result = self._answer(row)
            self.assertEqual(result["outcome"], "correct")
        with self.db._connect() as conn:
            states = conn.execute("SELECT direction,COUNT(*) FROM sentence_states GROUP BY direction").fetchall()
            self.assertEqual({row[0]: row[1] for row in states}, {"en_nl": 3, "nl_en": 3})
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(self.provider.graded, [])

    def test_original_forms_are_adapted_and_supporting_words_are_allowed(self) -> None:
        """Use saved words as targets rather than a restrictive word list; e.g. slim becomes slimste and neighbour can be newly supplied."""

        row = self.db.sentence_current(self._round())
        self.assertIn("slimste", row["content"]["dutch"])
        self.assertIn("fietsen", row["content"]["dutch"])
        self.assertIn("zou", row["content"]["dutch"])
        self.assertIn("buurman", row["content"]["dutch"])
        requested = self.provider.calls[0][0][0]["words"]
        self.assertEqual({word["lemma"] for word in requested}, {"slim", "fiets", "lopen"})
        with self.db._connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM vocab_words").fetchone()[0], 3)

    def test_submission_is_durable_idempotent_and_activity_counts_sentences_once(self) -> None:
        """Count one typed answer with several target words as one practice event; e.g. pending correction already quiets reminders."""

        session = self._round()
        self.assertEqual(self.db.sentence_summary(session)["shown"], 0)
        row = self.db.sentence_current(session)
        self.assertEqual(row, self.db.sentence_current(session))
        original = self.db.sentence_submit(row["id"], fixtures.DUTCH, 1234)
        again = self.db.sentence_submit(row["id"], "Another attempt", 9999)
        self.assertEqual(original, again)
        data = practice_activity.read(self.db)
        self.assertEqual((data.answers, data.distinct_words, data.week[-1].sentences, data.week[-1].sentence_en_nl), (1, 3, 1, 1))
        self.assertEqual(self.db.practice_last_answer(time.time() + 1), original["answered_at"])
        resumed = library_db.LibraryDb(self.db.path).sentence_current(session)
        self.assertEqual(resumed["answer_text"], fixtures.DUTCH)

    def test_degree_error_keeps_meaning_credit_and_other_word_successes(self) -> None:
        """Diagnose the requested grammatical distinction; e.g. comparative instead of superlative fails form, not the adjective's lexical meaning."""

        row = self.db.sentence_current(self._round())
        result = self._answer(row, fixtures.DUTCH.replace("slimste", "slimmere"))
        self.assertEqual(result["outcome"], "wrong")
        stats = sentence_stats.snapshot(self.db)
        words = {word["word"]: word for word in stats["terms"]}
        self.assertEqual((words["slim"]["meaning_correct"], words["slim"]["form_failures"], words["slim"]["correct"]), (1, 1, 0))
        self.assertEqual(words["fiets"]["correct"], 1)
        features = {word["feature"]: word for word in stats["forms"]}
        self.assertEqual(features["superlative"]["failures"], 1)
        self.assertEqual(features["adjective_ending"]["failures"], 0)
        rows = self.db.sentence_candidates("en_nl")
        selector = sentence_select.Selector(practice.ReviewPolicy())
        weights = selector.weights(rows, time.time())
        scored = {word["surface"]: weight for word, weight in zip(rows, weights)}
        self.assertGreater(scored["slim"], scored["fiets"])

    def test_valid_dutch_synonym_earns_sentence_credit_without_advancing_saved_target(self) -> None:
        """Preserve vocabulary-learning purpose alongside natural phrasing; e.g. intelligentste does not certify recall of slimste."""

        row = self.db.sentence_current(self._round())
        result = self._answer(row, fixtures.DUTCH.replace("slimste", "intelligentste"))
        self.assertEqual(result["outcome"], "correct")
        words = {item["surface"]: item for item in self.db.sentence_candidates("en_nl")}
        self.assertIsNone(words["slim"]["card_json"])
        self.assertEqual(words["slim"]["attempts"], 0)
        self.assertIsNotNone(words["fiets"]["card_json"])
        stats = {item["word"]: item for item in sentence_stats.snapshot(self.db)["terms"]}
        self.assertEqual((stats["slim"]["meaning_correct"], stats["slim"]["target_used"]), (1, 0))

    def test_valid_english_synonym_demonstrates_comprehension(self) -> None:
        """Accept meaningful English equivalents; e.g. smartest need not equal the model's cleverest example literally."""

        row = self.db.sentence_current(self._round(direction=practice.Direction.NL_EN))
        result = self._answer(row, fixtures.ENGLISH.replace("cleverest", "smartest"))
        self.assertEqual(result["outcome"], "correct")
        self.assertTrue(all(word["card_json"] for word in self.db.sentence_candidates("nl_en")))
        forms = {item["feature"]: item for item in sentence_stats.snapshot(self.db)["forms"]}
        self.assertEqual(forms["adjective_ending"]["attempts"], 0)

    def test_global_syntax_failure_returns_sentence_without_reinflating_known_words(self) -> None:
        """Keep sentence writing adaptive independently of lexical recall; e.g. retrying word order does not repeatedly upgrade future-due words."""

        now = datetime(2026, 11, 14, 12).timestamp()
        wrong = fixtures.DUTCH.replace("zou eventueel", "eventueel zou")
        with mock.patch("yt_whisper_subs.sentence_db.time.time", return_value=now):
            first = self.db.sentence_current(self._round())
            self._answer(first, wrong)
        with self.db._connect() as conn:
            before = [tuple(row) for row in conn.execute("SELECT * FROM sentence_states ORDER BY word_id")]
        with mock.patch("yt_whisper_subs.sentence_db.time.time", return_value=now + 61):
            second = self.db.sentence_current(self._round())
            self.assertEqual(first["question_id"], second["question_id"])
            self._answer(second, wrong)
        with self.db._connect() as conn:
            after = [tuple(row) for row in conn.execute("SELECT * FROM sentence_states ORDER BY word_id")]
            scheduled = conn.execute("SELECT SUM(scheduled) FROM sentence_assessments WHERE presentation_id=?", (second["id"],)).fetchone()[0]
        self.assertEqual(before, after)
        self.assertEqual(scheduled, 0)
        self.assertEqual(sentence_stats.snapshot(self.db)["wrong"], 2)

    def test_failed_or_uncertain_grading_keeps_one_pending_attempt(self) -> None:
        """Never convert provider failure into a learning failure; e.g. retry after an outage grades the same saved first sentence."""

        row = self.db.sentence_current(self._round())
        self.db.sentence_submit(row["id"], fixtures.DUTCH.replace("slimste", "slimmere"), 2000)
        self.provider.available = False
        with self.assertRaises(OSError):
            self.engine.check(row["id"], lambda _: None)
        self.provider.available = True
        result = self.provider.grade({"exercise": row["content"], "direction": row["direction"],
                                       "learner_answer": self.db.sentence_get(row["id"])["answer_text"]})
        self.provider.grade_result = result.model_copy(update={"confident": False})
        with self.assertRaisesRegex(ValueError, "uncertain"):
            self.engine.check(row["id"], lambda _: None)
        stats = sentence_stats.snapshot(self.db)
        self.assertEqual((stats["submitted"], stats["pending"], stats["wrong"]), (1, 1, 0))
        self.provider.grade_result = None
        self.engine.check(row["id"], lambda _: None)
        self.assertEqual(practice_activity.read(self.db).answers, 1)

    def test_report_retains_effort_and_removes_all_grading_influence(self) -> None:
        """Quarantine defective bilingual content; e.g. original typed evidence survives while word/form estimates and schedules rebuild."""

        row = self.db.sentence_current(self._round())
        self._answer(row, fixtures.DUTCH.replace("slimste", "slimmere"))
        self.db.sentence_flag(row["id"])
        self.assertEqual(sentence_stats.snapshot(self.db)["graded"], 0)
        self.assertEqual(practice_activity.read(self.db).answers, 1)
        self.assertEqual(self.db.sentence_get(row["id"])["outcome"], "invalid")
        with self.db._connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sentence_states").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sentence_question_states").fetchone()[0], 0)
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_unanswered_report_and_hidden_planning_add_no_activity(self) -> None:
        """Count actual effort only; e.g. reporting an unclear source before typing cannot suppress a reminder."""

        session = self._round()
        self.assertEqual(practice_activity.read(self.db).answers, 0)
        row = self.db.sentence_current(session)
        self.db.sentence_flag(row["id"])
        self.assertEqual(practice_activity.read(self.db).answers, 0)
        self.assertIsNone(self.db.practice_last_answer(time.time() + 1))

    def test_rejected_generation_and_provider_failure_preserve_previous_round(self) -> None:
        """Replace only after complete checked preparation; e.g. new-model generation errors cannot discard a live round."""

        session = self._round()
        self.provider.model = "new-policy"
        self.provider.approved = False
        with self.assertRaises(practice_service.NothingToPractice):
            self._round()
        self.assertEqual(len(self.provider.calls), 3)
        self.assertEqual(self.db.sentence_active(), session)
        self.provider.available = False
        with self.assertRaises(OSError):
            self._round()
        self.assertEqual(self.db.sentence_active(), session)

    def test_conflicting_or_fabricated_corrections_are_not_committed(self) -> None:
        """Ground feedback in actual learner text; e.g. a grader cannot quote an absent error and update mastery."""

        row = self.db.sentence_current(self._round())
        wrong = fixtures.DUTCH.replace("slimste", "slimmere")
        self.db.sentence_submit(row["id"], wrong, 2000)
        grade = self.provider.grade({"exercise": row["content"], "direction": row["direction"], "learner_answer": wrong})
        grade.errors[0].evidence = "Not in the sentence"
        with self.assertRaisesRegex(ValueError, "absent"):
            self.db.sentence_grade(row["id"], grade, {"model": "test", "reasoning": "low", "grade_version": 1})
        with self.db._connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sentence_assessments").fetchone()[0], 0)

    def test_approved_synonym_alternative_still_assesses_actual_target_use(self) -> None:
        """Do not infer saved-word recall from an alternative sentence; e.g. a preapproved intelligentste still leaves slim due."""

        generate = self.provider.generate

        def alternatives(*args):
            """Supply a genuine equivalent using another lexeme; e.g. audit approval alone cannot prove which word was recalled."""

            items = generate(*args)
            for item in items:
                item["draft"]["alternatives"] = [fixtures.DUTCH.replace("slimste", "intelligentste")]
            return items

        self.provider.generate = alternatives
        row = self.db.sentence_current(self._round())
        self._answer(row, row["content"]["alternatives"][0])
        self.assertEqual(len(self.provider.graded), 1)
        words = {word["surface"]: word for word in self.db.sentence_candidates("en_nl")}
        self.assertIsNone(words["slim"]["card_json"])

    def test_mastered_targets_wait_until_due_unless_early_is_requested(self) -> None:
        """Avoid boring repetitions after success; e.g. an optional early round is explicit rather than silently ignoring due dates."""

        self._answer(self.db.sentence_current(self._round()))
        with self.assertRaises(practice_service.NothingToPractice) as error:
            self._round()
        self.assertEqual(error.exception.reason, "not_due")
        self.assertIsNotNone(self.db.sentence_current(self._round(include_early=True)))

    def test_selection_uses_prior_vocabulary_difficulty_and_varied_contexts(self) -> None:
        """Seed sentence sampling from recognition without fabricating sentence attempts; e.g. prior failures increase probability for a new word."""

        rows = self.db.sentence_candidates("en_nl")
        selector = sentence_select.Selector(practice.ReviewPolicy(), np.random.default_rng(4))
        baseline = selector.weights(rows, time.time())
        rows[0].update(prior_attempts=10, prior_failures=10)
        self.assertGreater(selector.weights(rows, time.time())[0], baseline[0])
        opts = sentences.Options(count=3, words=2)
        plans = selector.plans(rows, rows, opts, {}, time.time())
        self.assertGreater(len({row["scene"] for row in plans}), 1)
        self.assertTrue(all(len({word["word_id"] for word in row["words"]}) == 2 for row in plans))

    @mock.patch("yt_whisper_subs.openai_client.responses_api_request")
    def test_generation_audit_and_grading_share_luna_strict_contracts(self, request) -> None:
        """Exercise the real structured provider boundary; e.g. generation and independent audit are separate requests with storage disabled."""

        rows = self.db.sentence_candidates("en_nl")
        opts = fixtures.options()
        selector = sentence_select.Selector(practice.ReviewPolicy())
        plan = selector.plans(rows[:1], rows, opts, {}, time.time())[0]
        document = self.engine._document(plan)
        item = self.provider.generate([document], opts.direction, [])[0]
        wrong = fixtures.DUTCH.replace("slimste", "slimmere")
        grade_doc = {"direction": "en_nl", "exercise": item["draft"], "learner_answer": wrong}
        grade = self.provider.grade(grade_doc)
        request.side_effect = [{"output_text": json.dumps({"questions": [item["draft"]]})},
                               {"output_text": json.dumps({f"q_{item['draft']['anchor_word_id']}":
                                                             {key: item["verdict"][key] for key in ("valid", "reason")}})},
                               {"output_text": grade.model_dump_json()}]
        provider = sentence_generate.SentenceGenerator()
        self.assertEqual(provider.generate([document], opts.direction, []), [item])
        result = provider.grade(grade_doc)
        result.check(wrong, item["draft"])
        self.assertEqual(request.call_count, 3)
        for call in request.call_args_list:
            args, payload = call.args
            self.assertEqual((payload["model"], payload["reasoning"]["effort"]), ("gpt-6-luna", "medium"))
            self.assertFalse(payload["store"])
            self.assertTrue(payload["text"]["format"]["strict"])
            self.assertEqual(args.openai_max_retries, 0)

    @mock.patch("yt_whisper_subs.openai_client.responses_api_request")
    def test_overlapping_targets_have_required_audit_slots_and_locally_bound_ids(self, request) -> None:
        """Require one actual decision per question; e.g. shared vocabulary and reversed response order cannot corrupt audit identity."""

        rows = self.db.sentence_candidates("en_nl")
        opts = fixtures.options().model_copy(update={"count": 3})
        plans = sentence_select.Selector(practice.ReviewPolicy()).plans(rows, rows, opts, {}, time.time())
        documents = [self.engine._document(plan) for plan in plans]
        items = self.provider.generate(documents, opts.direction, [])
        decisions = {f"q_{item['draft']['anchor_word_id']}": {"valid": idx != 1, "reason": f"Independent check {idx}"}
                     for idx, item in enumerate(reversed(items))}
        request.side_effect = [{"output_text": json.dumps({"questions": [item["draft"] for item in items]})},
                               {"output_text": json.dumps(decisions)}]
        result = sentence_generate.SentenceGenerator().generate(documents, opts.direction, [])
        for item in result:
            anchor = item["draft"]["anchor_word_id"]
            self.assertEqual(item["verdict"], {"anchor_word_id": anchor, **decisions[f"q_{anchor}"]})
        audit = request.call_args_list[-1].args[1]
        schema = audit["text"]["format"]["schema"]
        self.assertEqual(set(schema["required"]), set(decisions))
        self.assertEqual(set(json.loads(audit["input"])["questions"]), set(decisions))
        self.assertFalse(schema["additionalProperties"])
        self.assertNotIn("anchor_word_id", schema["$defs"]["Decision"]["properties"])

    @mock.patch("yt_whisper_subs.openai_client.responses_api_request")
    def test_incomplete_audit_retries_only_review_without_regenerating_paid_drafts(self, request) -> None:
        """Recover the reported failure once without inventing approvals; e.g. a missing slot triggers a corrected audit of the same content."""

        rows = self.db.sentence_candidates("en_nl")
        plan = sentence_select.Selector(practice.ReviewPolicy()).plans(rows[:1], rows, fixtures.options(), {}, time.time())[0]
        document = self.engine._document(plan)
        item = self.provider.generate([document], practice.Direction.EN_NL, [])[0]
        decision = {f"q_{item['draft']['anchor_word_id']}": {"valid": True, "reason": "Checked independently"}}
        request.side_effect = [{"output_text": json.dumps({"questions": [item["draft"]]})},
                               {"output_text": "{}"}, {"output_text": json.dumps(decision)}]
        result = sentence_generate.SentenceGenerator().generate([document], practice.Direction.EN_NL, [])
        self.assertTrue(result[0]["verdict"]["valid"])
        names = [call.args[1]["text"]["format"]["name"] for call in request.call_args_list]
        self.assertEqual(names, ["drafts", "sentenceaudit", "sentenceaudit"])
        self.assertIn("response_error", json.loads(request.call_args_list[-1].args[1]["input"]))

    @mock.patch("yt_whisper_subs.openai_client.responses_api_request")
    def test_repeated_incomplete_audit_is_bounded_and_never_creates_playable_content(self, request) -> None:
        """Fail closed after one response repair; e.g. the previous ready round survives two missing-audit responses."""

        session = self._round()

        def incomplete(_args, payload):
            """Return structurally incomplete reviews for the current real request; e.g. no matching-ID response is fabricated."""

            if payload["text"]["format"]["name"] != "drafts":
                return {"output_text": "{}"}
            data = json.loads(payload["input"])
            items = self.provider.generate(data["plans"], practice.Direction.EN_NL, [])
            return {"output_text": json.dumps({"questions": [item["draft"] for item in items]})}

        request.side_effect = incomplete
        engine = sentence_service.SentenceService(self.db, sentence_generate.SentenceGenerator())
        with self.assertRaisesRegex(ValueError, "incomplete after retry"):
            engine.start(fixtures.options(), lambda _: None)
        self.assertEqual(request.call_count, 3)
        self.assertEqual(self.db.sentence_active(), session)
        with self.db._connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sentence_questions").fetchone()[0], 1)

    @mock.patch("yt_whisper_subs.openai_client.responses_api_request")
    def test_invalid_form_evidence_is_repaired_without_discarding_other_questions(self, request) -> None:
        """Treat local metadata defects as content rejections; e.g. absent English evidence repairs one draft and preserves the other paid approvals."""

        events = []
        bad_anchor = None

        def respond(_args, payload):
            """Simulate one bad form annotation followed by a valid repair; e.g. all auditing still uses the real strict schema."""

            nonlocal bad_anchor
            data = json.loads(payload["input"])
            name = payload["text"]["format"]["name"]
            events.append((name, data))
            if name == "drafts":
                items = self.provider.generate(data["plans"], practice.Direction.EN_NL, [])
                if bad_anchor is None:
                    bad_anchor = items[0]["draft"]["anchor_word_id"]
                    items[0]["draft"]["words"][0]["english_forms"] = ["absent invented annotation"]
                return {"output_text": json.dumps({"questions": [item["draft"] for item in items]})}
            return {"output_text": json.dumps({key: {"valid": True, "reason": "Independently checked"} for key in data["questions"]})}

        request.side_effect = respond
        engine = sentence_service.SentenceService(self.db, sentence_generate.SentenceGenerator())
        opts = fixtures.options().model_copy(update={"count": 3})
        session = engine.start(opts, lambda _: None)
        generations = [doc for name, doc in events if name == "drafts"]
        self.assertEqual([len(doc["plans"]) for doc in generations], [3, 1])
        self.assertEqual(generations[1]["plans"][0]["anchor_word_id"], bad_anchor)
        self.assertIn("absent", generations[1]["plans"][0]["previous_rejection"])
        self.assertEqual(self.db.sentence_summary(session)["planned"], 3)
        self.assertEqual(self.db.sentence_summary(session)["shown"], 0)
        with self.db._connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sentence_questions").fetchone()[0], 3)


if __name__ == "__main__":
    unittest.main()
