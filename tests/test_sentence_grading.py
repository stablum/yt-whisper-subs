"""Verify bounded correction repair without weakening learning evidence.

Example: a sentence-level word-order error must not fail a correctly recalled vocabulary target.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import sentence_fixtures as fixtures
from yt_whisper_subs import library_db
from yt_whisper_subs import practice_activity
from yt_whisper_subs import sentence_generate
from yt_whisper_subs import sentence_service


class SentenceGradingTests(unittest.TestCase):
    """Use the real provider and grade transaction with mocked transport; e.g. one submitted answer gets at most one repair request."""

    def setUp(self):
        """Create a syntax-only mistake with correctly recalled targets; e.g. rejecting bad metadata cannot alter its first attempt."""

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = library_db.LibraryDb(Path(folder.name) / "catalog.sqlite3")
        self.db.initialize()
        fixtures.seed(self.db)
        provider = fixtures.FakeSentenceProvider()
        engine = sentence_service.SentenceService(self.db, provider)
        self.session = engine.start(fixtures.options(), lambda _: None)
        self.row = self.db.sentence_current(self.session)
        self.answer = fixtures.DUTCH.replace("zou eventueel", "eventueel zou")
        self.db.sentence_submit(self.row["id"], self.answer, 2500)
        document = {"exercise": self.row["content"], "direction": "en_nl", "learner_answer": self.answer}
        self.grade = provider.grade(document)
        self.bad = self.grade.model_copy(deep=True)
        self.bad.errors[0].word_ids = [self.bad.words[0].word_id]
        self.engine = sentence_service.SentenceService(self.db, sentence_generate.SentenceGenerator())
        patch = mock.patch("yt_whisper_subs.openai_client.responses_api_request")
        self.request = patch.start()
        self.addCleanup(patch.stop)

    @staticmethod
    def _response(grade):
        """Encode the real strict-schema response boundary; e.g. tests validate both JSON and semantic diagnostics."""

        return {"output_text": grade.model_dump_json()}

    def test_conflicting_error_link_is_repaired_without_false_word_failures(self):
        """Reconcile classification rather than discard an error; e.g. correct vocabulary keeps credit despite a real sentence syntax failure."""

        self.request.side_effect = [self._response(self.bad), self._response(self.grade)]
        result = self.engine.check(self.row["id"], lambda _: None)
        self.assertEqual(result["outcome"], "wrong")
        self.assertEqual(result["answer_text"], self.answer)
        self.assertEqual(result["grade"]["errors"], [error.model_dump() for error in self.grade.errors])
        self.assertTrue(all(word["meaning_correct"] and word["form_correct"] for word in result["grade"]["words"]))
        first, repair = [json.loads(call.args[1]["input"]) for call in self.request.call_args_list]
        self.assertEqual(repair["learner_answer"], first["learner_answer"])
        self.assertEqual(repair["exercise"], first["exercise"])
        self.assertIn(str(self.bad.words[0].word_id), repair["previous_rejection"])
        self.assertEqual(repair["previous_proposal"], self.bad.model_dump())
        self.assertEqual(practice_activity.read(self.db).answers, 1)
        self.engine.check(self.row["id"], lambda _: None)
        self.assertEqual(self.request.call_count, 2)
        with self.db._connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sentence_assessments").fetchone()[0], 3)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sentence_errors").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sentence_error_words").fetchone()[0], 0)

    def test_repeated_inconsistency_is_bounded_and_leaves_history_pending(self):
        """Stop after one repair without inventing scores; e.g. a permanently contradictory provider cannot keep spending or corrupt mastery."""

        self.request.return_value = self._response(self.bad)
        with self.assertRaisesRegex(ValueError, "inconsistent after repair"):
            self.engine.check(self.row["id"], lambda _: None)
        self.assertEqual(self.request.call_count, 2)
        row = self.db.sentence_get(self.row["id"])
        self.assertEqual(row["answer_text"], self.answer)
        self.assertIsNone(row["outcome"])
        self.assertIsNone(row["grade"])
        self.assertEqual(practice_activity.read(self.db).answers, 1)
        with self.db._connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sentence_assessments").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sentence_question_states").fetchone()[0], 0)

    def test_incomplete_response_gets_one_schema_repair(self):
        """Recover malformed output at the same boundary; e.g. a missing field is not a learner mistake or a new exercise."""

        self.request.side_effect = [{"output_text": "{}"}, self._response(self.grade)]
        self.assertEqual(self.engine.check(self.row["id"], lambda _: None)["outcome"], "wrong")
        self.assertEqual(self.request.call_count, 2)
        repair = json.loads(self.request.call_args.args[1]["input"])
        self.assertIn("previous_rejection", repair)
        self.assertNotIn("previous_proposal", repair)

    def test_invalid_source_alignment_can_be_repaired_in_both_directions(self):
        """Apply consistency checks before returning a provider grade; e.g. English and Dutch source spans have the same protection."""

        for direction in ("en_nl", "nl_en"):
            with self.subTest(direction=direction):
                answer = self.answer if direction == "en_nl" else fixtures.ENGLISH.replace("would possibly", "possibly would")
                document = {"exercise": self.row["content"], "direction": direction, "learner_answer": answer}
                grade = fixtures.FakeSentenceProvider().grade(document)
                invalid = grade.model_copy(deep=True)
                invalid.errors[0].source_forms = ["fabricated source span"]
                self.request.reset_mock(side_effect=True)
                self.request.side_effect = [self._response(invalid), self._response(grade)]
                result = sentence_generate.SentenceGenerator().grade(document)
                result.check(answer, self.row["content"], direction)
                self.assertEqual(self.request.call_count, 2)

    def test_uncertainty_is_not_retried_into_false_confidence(self):
        """Leave ambiguous grading for review; e.g. the repair loop fixes contracts, not the model's lack of confidence."""

        grade = self.grade.model_copy(update={"confident": False})
        self.request.return_value = self._response(grade)
        with self.assertRaisesRegex(ValueError, "uncertain"):
            self.engine.check(self.row["id"], lambda _: None)
        self.assertEqual(self.request.call_count, 1)
        self.assertIsNone(self.db.sentence_get(self.row["id"])["outcome"])

    def test_network_failure_is_not_an_automatic_paid_retry(self):
        """Keep transport failures under explicit user control; e.g. an outage never triggers semantic repair."""

        self.request.side_effect = OSError("Provider unavailable")
        with self.assertRaisesRegex(OSError, "Provider unavailable"):
            self.engine.check(self.row["id"], lambda _: None)
        self.assertEqual(self.request.call_count, 1)
        self.assertIsNone(self.db.sentence_get(self.row["id"])["outcome"])

    def test_valid_feedback_needs_only_one_request(self):
        """Keep normal corrections fast and economical; e.g. the repair budget is unused when diagnostics already agree."""

        self.request.return_value = self._response(self.grade)
        self.assertEqual(self.engine.check(self.row["id"], lambda _: None)["outcome"], "wrong")
        self.assertEqual(self.request.call_count, 1)
