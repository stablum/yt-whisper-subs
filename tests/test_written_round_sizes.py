"""Verify the shared round contract in both written exercise pipelines.

Example: failed checks in the first batch cannot trigger retries before the remaining initial targets are attempted.
"""

import tempfile
import unittest
from pathlib import Path

from tests import verb_fixtures
from tests import sentence_fixtures
from yt_whisper_subs import library_db
from yt_whisper_subs import practice_round
from yt_whisper_subs import sentence_practice as sentences
from yt_whisper_subs import sentence_service
from yt_whisper_subs import verb_service


class WrittenRoundSizeTests(unittest.TestCase):
    """Exercise both real written services; e.g. all round and cache writes go through production SQLite code."""

    def _case(self, kind):
        """Keep mode-specific setup together; e.g. verb exercises use starter targets while sentences use saved vocabulary."""

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = library_db.LibraryDb(Path(tmp.name) / "catalog.sqlite3")
        db.initialize()
        if kind == "verb":
            provider = verb_fixtures.FakeVerbProvider()
            opts = verb_fixtures.options(5).model_copy(update={"source": "starter"})
            engine = verb_service.VerbService(db, provider)
        else:
            entries = [{"surface": f"woord{i}", "lemma": f"woord{i}", "pos": "noun", "meaning": f"term {i}"}
                       for i in range(12)]
            verb_fixtures.seed_vocab(db, entries)
            provider = sentence_fixtures.SimpleSentenceProvider()
            opts = sentences.Options(count=5, words=2)
            engine = sentence_service.SentenceService(db, provider)
        return db, engine, opts

    def test_initial_targets_precede_replacements_in_both_modes(self):
        """Attempt N before topping up its deficit; e.g. rejecting the first four yields batches 4, 1, 4 rather than a premature repair batch."""

        for kind in ("verb", "sentence"):
            with self.subTest(kind=kind):
                db, engine, opts = self._case(kind)
                original, calls = engine._provider.generate, []

                def generate(rows, *args):
                    """Reject only the initial batch; e.g. the fifth original target must still be prepared next."""

                    calls.append(len(rows))
                    results = original(rows, *args)
                    for item in results:
                        item["verdict"]["valid"] = len(calls) > 1
                    return results

                engine._provider.generate = generate
                session = engine.start(opts, lambda _: None)
                self.assertEqual(calls, [4, 1, 4])
                self.assertEqual(getattr(db, f"{kind}_summary")(session)["planned"], 5)

    def test_one_rejection_generates_only_one_replacement_in_both_modes(self):
        """Retain all other approvals; e.g. five requested exercises cost six drafts after one failed quality check."""

        for kind in ("verb", "sentence"):
            with self.subTest(kind=kind):
                db, engine, opts = self._case(kind)
                original, calls = engine._provider.generate, []

                def generate(rows, *args):
                    """Reject one item exactly once; e.g. a later replacement is approved normally."""

                    calls.append(len(rows))
                    results = original(rows, *args)
                    if len(calls) == 1:
                        results[0]["verdict"]["valid"] = False
                    return results

                engine._provider.generate = generate
                session = engine.start(opts, lambda _: None)
                self.assertEqual(calls, [4, 1, 1])
                self.assertEqual(getattr(db, f"{kind}_summary")(session)["planned"], 5)

    def test_total_guard_preserves_previous_round_and_approved_cache(self):
        """Share one quota across all batches and top-ups; e.g. five requested questions cannot spend more than ten drafts on repeated rejection."""

        for kind in ("verb", "sentence"):
            with self.subTest(kind=kind):
                db, engine, opts = self._case(kind)
                previous = engine.start(opts.model_copy(update={"count": 1}), lambda _: None)
                engine._provider.model = "new-round-policy"
                original, calls = engine._provider.generate, []

                def generate(rows, *args):
                    """Keep one approval, then fail all checks; e.g. successful paid content must survive exhaustion."""

                    calls.append(len(rows))
                    results = original(rows, *args)
                    for idx, item in enumerate(results):
                        item["verdict"]["valid"] = len(calls) == 1 and idx == 0
                    return results

                engine._provider.generate = generate
                with self.assertRaises(practice_round.NothingToPractice):
                    engine.start(opts, lambda _: None)
                self.assertEqual(sum(calls), 10)
                self.assertEqual(getattr(db, f"{kind}_active")(), previous)
                with db._connect() as conn:
                    cached = conn.execute(f"SELECT COUNT(*) FROM {kind}_questions WHERE model='new-round-policy'").fetchone()[0]
                self.assertEqual(cached, 1)
                calls.clear()

                def recover(rows, *args):
                    """Reuse paid approvals on the next try; e.g. only four new drafts are needed after the guard stops preparation."""

                    calls.append(len(rows))
                    return original(rows, *args)

                engine._provider.generate = recover
                session = engine.start(opts, lambda _: None)
                self.assertEqual(sum(calls), 4)
                self.assertEqual(getattr(db, f"{kind}_summary")(session)["planned"], 5)
