"""Verify exact round sizes, incremental top-ups and the hidden total attempt guard.

Example: 35 requested with one rejection submits 35 initial drafts and one replacement, never 70 upfront.
"""

import tempfile
import unittest
from pathlib import Path

from tests import verb_fixtures
from yt_whisper_subs import library_db
from yt_whisper_subs import practice
from yt_whisper_subs import practice_round
from yt_whisper_subs import practice_service


class ControlledProvider:
    """Make acceptance and submitted work observable; e.g. tests simulate strict reviews without spending API credits."""

    model, reasoning = "exact-round-test", "low"

    def __init__(self) -> None:
        """Track submitted targets independently of API batch count; e.g. 36 drafts can arrive in seven calls."""

        self.calls = []
        self.reject_first = False
        self.accept_limit = None
        self.omit_once = False
        self._accepted = 0

    def generate(self, rows, direction):
        """Return controlled but structurally valid questions; e.g. one rejected target gets a different replacement."""

        self.calls.append([row["lookup_id"] for row in rows])
        results = []
        for row in rows:
            valid = not self.reject_first and (self.accept_limit is None or self._accepted < self.accept_limit)
            self.reject_first = False
            self._accepted += int(valid)
            draft = practice.Draft(lookup_id=row["lookup_id"], explanation="A controlled unambiguous test question.",
                                    distractors=[practice.Distractor(text=f"wrong {idx}",explanation="Another concept.",kind="other")
                                                 for idx in (1,2)])
            verdict = practice.Verdict(lookup_id=row["lookup_id"],valid=valid,reason="Controlled quality decision.")
            results.append({"draft":draft.model_dump(),"verdict":verdict.model_dump()})
        if self.omit_once:
            self.omit_once = False
            results.pop()
        return results


class ExactRoundTests(unittest.TestCase):
    """Exercise real cache, selection and durable sessions; e.g. partial success never replaces an earlier active round."""

    def setUp(self) -> None:
        """Seed more distinct targets than either quota; e.g. replacement shortages cannot hide budget errors."""

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = library_db.LibraryDb(Path(tmp.name)/"catalog.sqlite3")
        self.db.initialize()
        entries = [{"surface":f"woord{i}","lemma":f"woord{i}","pos":"noun","meaning":f"term {i}",
                    "sentence":f"Ik leer woord{i} vandaag.","event":f"exact:{i}"} for i in range(80)]
        verb_fixtures.seed_vocab(self.db,entries)
        self.provider = ControlledProvider()
        self.engine = practice_service.PracticeService(self.db,self.provider)
        self.opts = practice.RoundOptions(direction=practice.Direction.NL_EN,count=35)

    def test_one_rejection_adds_only_one_replacement(self) -> None:
        """Top up the deficit instead of enlarging the initial request; e.g. 34 approvals plus one new question give exactly 35."""

        self.provider.reject_first = True
        session = self.engine.start(self.opts,lambda _:None)
        self.assertEqual(self.db.practice_summary(session)["planned"],35)
        self.assertEqual(sum(map(len,self.provider.calls)),36)
        self.assertEqual(len(self.provider.calls[-1]),1)
        self.assertEqual(len(set(sum(self.provider.calls,[]))),36)
        self.assertEqual(self.db.practice_summary(session)["shown"],0)

    def test_clean_initial_request_does_not_generate_a_reserve(self) -> None:
        """Spend only the requested amount when checks succeed; e.g. no hidden 70-question preflight exists."""

        session = self.engine.start(self.opts,lambda _:None)
        self.assertEqual(self.db.practice_summary(session)["planned"],35)
        self.assertEqual(sum(map(len,self.provider.calls)),35)

    def test_partial_cache_generates_only_the_missing_question(self) -> None:
        """Consume already paid approved stock; e.g. 34 unseen cached questions need one generation after a stopped round."""

        session = self.engine.start(self.opts,lambda _:None)
        shown = self.db.practice_current(session)
        self.db.practice_finish(session)
        with self.db._connect() as conn:
            conn.execute("UPDATE vocab_lookups SET ambiguous=1 WHERE id=?",(shown["lookup_id"],))
        self.provider.calls.clear()
        following = self.engine.start(self.opts,lambda _:None,cached_first=True)
        self.assertEqual(self.db.practice_summary(following)["planned"],35)
        self.assertEqual(sum(map(len,self.provider.calls)),1)

    def test_total_guard_preserves_approvals_and_previous_round(self) -> None:
        """Cap submitted draft attempts across batches; e.g. repeated rejection cannot create a short round or discard checked work."""

        old = self.engine.start(self.opts.model_copy(update={"count":1}),lambda _:None)
        self.provider.model = "new-exact-policy"
        self.provider.calls.clear()
        self.provider._accepted = 0
        self.provider.accept_limit = 1
        with self.assertRaises(practice_round.NothingToPractice) as failure:
            self.engine.start(self.opts,lambda _:None)
        self.assertEqual(sum(map(len,self.provider.calls)),70)
        self.assertEqual(failure.exception.reason,"no_questions")
        self.assertNotIn("70",str(failure.exception))
        self.assertEqual(self.db.practice_active(),old)
        self.provider.accept_limit = None
        self.provider.calls.clear()
        new = self.engine.start(self.opts,lambda _:None,cached_first=True)
        self.assertEqual(self.db.practice_summary(new)["planned"],35)
        self.assertEqual(sum(map(len,self.provider.calls)),34)

    def test_insufficient_distinct_targets_spends_no_tokens(self) -> None:
        """Fail honestly when the selection cannot meet the requested size; e.g. no paid partial round is prepared first."""

        with self.db._connect() as conn:
            conn.execute("UPDATE vocab_lookups SET ambiguous=1 WHERE id>4")
        with self.assertRaises(practice_round.NothingToPractice) as failure:
            self.engine.start(self.opts,lambda _:None)
        self.assertEqual(failure.exception.reason,"not_enough")
        self.assertEqual(self.provider.calls,[])
        self.assertIsNone(self.db.practice_active())

    def test_malformed_batch_is_replaced_within_the_same_quota(self) -> None:
        """Treat missing response coverage as bounded failed work; e.g. approvals are never inferred for omitted questions."""

        self.provider.omit_once = True
        session = self.engine.start(self.opts,lambda _:None)
        self.assertEqual(self.db.practice_summary(session)["planned"],35)
        self.assertEqual(sum(map(len,self.provider.calls)),41)

    def test_startup_discards_only_unshown_short_rounds(self) -> None:
        """Preserve actual learner history while repairing ready-stock invariants; e.g. an unused 34-of-35 cache cannot be resumed as complete."""

        session = self.engine.start(self.opts,lambda _:None)
        with self.db._connect() as conn:
            conn.execute("DELETE FROM practice_presentations WHERE session_id=? AND ordinal=35",(session,))
        self.db.initialize()
        self.assertIsNone(self.db.practice_active())
        self.provider.calls.clear()
        following = self.engine.start(self.opts,lambda _:None,cached_first=True)
        self.assertEqual(self.db.practice_summary(following)["planned"],35)
        self.assertEqual(self.provider.calls,[])
        self.db.practice_current(following)
        with self.db._connect() as conn:
            conn.execute("DELETE FROM practice_presentations WHERE session_id=? AND ordinal=35",(following,))
        self.db.initialize()
        self.assertEqual(self.db.practice_active(),following)
