"""Check gradual sentence credit from immutable bilingual feedback.

Example: a small grammar error can yield a high partial score while full-answer accuracy stays at zero.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import sentence_fixtures as fixtures
from yt_whisper_subs import library_db
from yt_whisper_subs import practice
from yt_whisper_subs import sentence_score
from yt_whisper_subs import sentence_service
from yt_whisper_subs import sentence_stats


class PartialScoreTests(unittest.TestCase):
    """Measure words rather than characters or vocabulary-only targets; e.g. omissions and extra words also cost credit."""

    def test_one_changed_word_in_ten_earns_ninety_percent(self):
        """Give useful credit before a sentence is perfect; e.g. an adjective ending changes one word, not the whole result."""

        self.assertAlmostEqual(sentence_score.partial("one two three four five six seven eight nine wrong",
                                                      "one two three four five six seven eight nine ten"), 0.9)

    def test_insertions_deletions_order_and_spelling_affect_the_score(self):
        """Count minimal word edits consistently; e.g. a word missing from the answer cannot be counted as correct."""

        cases = [("ik fiets", "ik fiets graag", 2 / 3), ("ik fiets graag", "ik fiets", 2 / 3),
                 ("morgen ga ik", "ik ga morgen", 1 / 3), ("ik fets graag", "ik fiets graag", 2 / 3),
                 ("helemaal andere woorden", "ik fiets graag", 0)]
        for answer, corrected, expected in cases:
            with self.subTest(answer=answer, corrected=corrected):
                self.assertAlmostEqual(sentence_score.partial(answer, corrected), expected)

    def test_harmless_case_punctuation_spacing_and_unicode_are_ignored(self):
        """Match the existing grading normalization; e.g. decomposed café and its NFC spelling earn the same credit."""

        self.assertEqual(sentence_score.partial('  IK loop naar het cafe\u0301! ', 'Ik loop naar het café.'), 1)


class SentenceScoreStatsTests(unittest.TestCase):
    """Read actual SQLite histories without regrading; e.g. historical corrections gain partial scores immediately."""

    def setUp(self):
        """Seed real saved targets with an offline linguistic provider; e.g. no learner data or API tokens are needed."""

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = library_db.LibraryDb(Path(folder.name) / "catalog.sqlite3")
        self.db.initialize()
        fixtures.seed(self.db)
        self.engine = sentence_service.SentenceService(self.db, fixtures.FakeSentenceProvider())

    def _attempt(self, answer, direction=practice.Direction.EN_NL, *, grade=True):
        """Create one saved first attempt; e.g. different rounds retain separate averaging denominators."""

        opts = fixtures.options(direction=direction).model_copy(update={"include_early": True})
        session = self.engine.start(opts, lambda _: None)
        row = self.db.sentence_current(session)
        self.db.sentence_submit(row["id"], answer, 1000)
        return self.engine.check(row["id"], lambda _: None) if grade else self.db.sentence_get(row["id"])

    def test_historical_nonperfect_answer_gets_partial_credit_without_regrading(self):
        """Keep strict success and gradual progress together; e.g. a missed superlative is 0% whole-sentence but 89% partial credit."""

        row = self._attempt(fixtures.DUTCH.replace("slimste", "slimmere"))
        with mock.patch.object(self.engine._provider, "grade", side_effect=AssertionError("No regrading")):
            data = sentence_stats.snapshot(self.db)
        self.assertEqual((data["correct"], data["wrong"]), (0, 1))
        self.assertAlmostEqual(data["partial_score"], 8 / 9)
        self.assertAlmostEqual(data["daily"][0]["partial_score"], 8 / 9)
        self.assertAlmostEqual(data["rounds"][0]["partial_score"], 8 / 9)
        self.assertAlmostEqual(data["scenes"][0]["partial_score"], 8 / 9)
        self.assertAlmostEqual(data["answers"][0]["partial_score"], 8 / 9)
        self.assertEqual(self.db.sentence_get(row["id"]), row)

    def test_daily_direction_and_global_averages_count_each_sentence_equally(self):
        """Avoid making longer answers dominate progress; e.g. both directions retain distinct dated series and one shared mean."""

        first = self._attempt(fixtures.DUTCH.replace("slimste", "slimmere"))
        self._attempt(fixtures.ENGLISH, practice.Direction.NL_EN)
        data = sentence_stats.snapshot(self.db)
        self.assertAlmostEqual(data["partial_score"], (8 / 9 + 1) / 2)
        directions = {row["direction"]: row for row in data["daily"]}
        self.assertAlmostEqual(directions["en_nl"]["partial_score"], 8 / 9)
        self.assertEqual(directions["nl_en"]["partial_score"], 1)
        self.assertEqual(len(data["answers"]), 2)
        self.assertEqual(len(data["rounds"]), 2)
        self.assertTrue(any(row["id"] == first["session_id"] for row in data["rounds"]))

    def test_accepted_synonym_is_compared_with_its_own_correction(self):
        """Respect accepted wording instead of enforcing one reference; e.g. intelligentste earns 100% despite differing from stored slimste."""

        self._attempt(fixtures.DUTCH.replace("slimste", "intelligentste"))
        data = sentence_stats.snapshot(self.db)
        self.assertEqual((data["correct"], data["partial_score"]), (1, 1))

    def test_empty_pending_and_reported_answers_have_no_invented_zero_scores(self):
        """Exclude unavailable or disputed feedback; e.g. a slow correction cannot create a fake drop in the daily chart."""

        self.assertIsNone(sentence_stats.snapshot(self.db)["partial_score"])
        graded = self._attempt(fixtures.DUTCH.replace("slimste", "slimmere"))
        pending = self._attempt(fixtures.DUTCH, grade=False)
        self.db.sentence_flag(graded["id"])
        data = sentence_stats.snapshot(self.db)
        self.assertIsNone(data["partial_score"])
        self.assertEqual((data["daily"], data["answers"]), ([], []))
        self.assertEqual(data["pending"], 1)
        self.assertTrue(all(row["partial_score"] is None for row in data["rounds"]))
        self.assertEqual(self.db.sentence_get(pending["id"])["answer_text"], fixtures.DUTCH)
