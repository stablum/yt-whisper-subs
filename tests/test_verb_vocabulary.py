"""Verify captured-verb provenance, lexical validation, source filters and sentence meanings.

Example: `python -m unittest tests.test_verb_vocabulary` uses genuine SQLite lookups and an offline provider.
"""

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests import verb_fixtures
from yt_whisper_subs import practice_round
from yt_whisper_subs import library_db
from yt_whisper_subs import practice
from yt_whisper_subs import verb_generate
from yt_whisper_subs import verb_practice as verbs
from yt_whisper_subs import verb_service
from yt_whisper_subs import verb_syllabus
from yt_whisper_subs import verb_vocabulary


class VocabularyVerbTests(unittest.TestCase):
    """Exercise the real vocabulary-to-lexicon boundary; e.g. source clicks remain linked after normalization."""

    def setUp(self) -> None:
        """Keep histories and providers isolated; e.g. no user catalog or API credentials are touched."""

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = library_db.LibraryDb(Path(tmp.name) / "catalog.sqlite3")
        self.db.initialize()
        self.provider = verb_fixtures.FakeVerbProvider()
        self.sync = verb_vocabulary.VocabularySync(self.db, self.provider)
        self.engine = verb_service.VerbService(self.db, self.provider)

    def test_inflected_forms_and_repeated_videos_share_one_starter_verb(self) -> None:
        """Reuse complete stored infinitives without extra calls; e.g. liep and gelopen increase the same lopen priority."""

        ids = verb_fixtures.seed_vocab(self.db, [
            {"surface": "liep", "lemma": "lopen", "video": "a"},
            {"surface": "gelopen", "lemma": "lopen", "pos": "verb (past participle)", "video": "b"},
            {"surface": "liep", "lemma": "lopen", "video": "c"},
            {"surface": "huis", "lemma": "huis", "pos": "noun"},
            {"surface": "stond", "lemma": "staan", "ambiguous": True},
            {"surface": "walked", "lemma": "walk", "language": "en"},
        ])
        self.provider.available = False
        self.sync.sync(lambda _: None)
        self.sync.sync(lambda _: None)
        catalog = self.db.verb_catalog()
        row = next(row for row in catalog if row["lemma"] == "lopen")
        self.assertEqual((row["lookups"], row["clicks"], row["videos"]), (2, 3, 3))
        self.assertEqual(len(catalog), 36)
        self.assertEqual(ids[0], ids[2])
        self.assertEqual(self.provider.lexical_calls, [])
        with self.db._connect() as conn:
            linked = conn.execute("SELECT lookup_id FROM verb_vocab_links WHERE verb_id=?", (row["id"],)).fetchall()
        self.assertEqual({item[0] for item in linked}, {ids[0], ids[1]})

    def test_new_complete_infinitives_are_audited_deduplicated_and_cached(self) -> None:
        """Resolve new, reflexive and separated verbs once; e.g. a clicked op particle links to complete opbellen."""

        verb_fixtures.seed_vocab(self.db, [
            {"surface": "fietste", "lemma": "fietsen"},
            {"surface": "gefietst", "lemma": "fietsen", "pos": "past participle"},
            {"surface": "vergiste", "lemma": "vergissen", "sentence": "Hij vergiste zich gisteren."},
            {"surface": "op", "lemma": "bellen", "pos": "separable verb particle", "sentence": "Ik bel haar op."},
        ])
        self.sync.sync(lambda _: None)
        rows = {row["lemma"]: row for row in self.db.verb_catalog()}
        self.assertEqual(rows["fietsen"]["lookups"], 2)
        self.assertFalse(rows["fietsen"]["starter"])
        self.assertEqual(rows["zich vergissen"]["family"], "reflexive")
        self.assertEqual(rows["opbellen"]["family"], "separable")
        self.assertEqual(len(self.provider.lexical_calls), 1)
        self.provider.available = False
        self.sync.sync(lambda _: None)
        self.assertEqual(len(self.provider.lexical_calls), 1)
        with self.db._connect() as conn:
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            evidence = json.loads(conn.execute("SELECT evidence_json FROM verb_vocab_links LIMIT 1").fetchone()[0])
        self.assertEqual(evidence["strategy"], "provider_audit")

    def test_contextual_rejection_is_cached_and_not_practised(self) -> None:
        """Do not trust a misleading part-of-speech tag alone; e.g. an audited noun must not become a verb."""

        verb_fixtures.seed_vocab(self.db, [{"surface": "huis", "lemma": "huis", "pos": "verb"}])
        self.sync.sync(lambda _: None)
        self.sync.sync(lambda _: None)
        self.assertEqual(len(self.provider.lexical_calls), 1)
        opts = verb_fixtures.options(1).model_copy(update={"source": "vocabulary"})
        with self.assertRaises(practice_round.NothingToPractice):
            self.engine.start(opts, lambda _: None)
        self.assertNotIn("huis", {row["lemma"] for row in self.db.verb_catalog()})

    def test_invalid_batch_ids_cannot_partially_link_accepted_words(self) -> None:
        """Validate the complete batch before SQL writes; e.g. duplicate extraction IDs cannot import the first result anyway."""

        verb_fixtures.seed_vocab(self.db, [{"surface": "fietste", "lemma": "fietsen"},
                                           {"surface": "knutselde", "lemma": "knutselen"}])
        original = self.provider.lexemes

        def duplicate(rows):
            """Emulate a malformed structured result; e.g. the second decision repeats the first ID."""

            items = original(rows)
            return [items[0], items[0]]

        self.provider.lexemes = duplicate
        with self.assertRaisesRegex(ValueError, "IDs"):
            self.sync.sync(lambda _: None)
        with self.db._connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM verb_vocab_links").fetchone()[0], 0)

    def test_changed_lookup_cannot_receive_a_stale_lexical_decision(self) -> None:
        """Check the original source stamp at commit; e.g. a retranslation finishing during the audit invalidates its result."""

        ids = verb_fixtures.seed_vocab(self.db, [{"surface": "fietste", "lemma": "fietsen"}])
        original = self.provider.lexemes

        def changed(rows):
            """Advance the lookup while the provider responds; e.g. model the normal background translator race."""

            items = original(rows)
            with self.db._connect() as conn:
                conn.execute("UPDATE vocab_lookups SET updated_at=updated_at+1 WHERE id=?", (ids[0],))
            return items

        self.provider.lexemes = changed
        self.sync.sync(lambda _: None)
        self.assertFalse(any(row["lookups"] for row in self.db.verb_catalog()))
        self.provider.lexemes = original
        self.sync.sync(lambda _: None)
        self.assertTrue(any(row["lemma"] == "fietsen" and row["lookups"] for row in self.db.verb_catalog()))

    def test_source_filters_and_weighting_prefer_vocabulary_without_dropping_starters(self) -> None:
        """Expose both origins clearly; e.g. vocabulary-only testing excludes every uncaptured starter verb."""

        verb_fixtures.seed_vocab(self.db, [{"surface": "fietste", "lemma": "fietsen"}])
        self.sync.sync(lambda _: None)
        catalog = self.db.verb_catalog()
        opts = verb_fixtures.options(50)
        rows = verb_syllabus.candidates(catalog, [], opts)
        by_lemma = {row["lemma"]: row for row in rows}
        weights = verb_syllabus.Selector(practice.ReviewPolicy()).weights([by_lemma["fietsen"], by_lemma["werken"]], [], time.time())
        self.assertGreater(weights[0], 3 * weights[1])
        only = verb_syllabus.candidates(catalog, [], opts.model_copy(update={"source": "vocabulary"}))
        starter = verb_syllabus.candidates(catalog, [], opts.model_copy(update={"source": "starter"}))
        self.assertEqual({row["lemma"] for row in only}, {"fietsen"})
        self.assertEqual(len({row["lemma"] for row in starter}), 36)
        self.assertNotIn("fietsen", {row["lemma"] for row in starter})

    def test_vocabulary_only_round_uses_the_captured_lemma(self) -> None:
        """Run through the real round service; e.g. the saved fietsen becomes an exercise target rather than a starter substitute."""

        verb_fixtures.seed_vocab(self.db, [{"surface": "fietste", "lemma": "fietsen"}])
        opts = verb_fixtures.options(1).model_copy(update={"source": "vocabulary"})
        session = self.engine.start(opts, lambda _: None)
        row = self.db.verb_current(session)
        self.assertEqual(row["lemma"], "fietsen")
        self.assertEqual({target["lemma"] for target in self.provider.targets}, {"fietsen"})

    def test_supplemental_sentence_help_is_cached_without_rewriting_an_existing_question(self) -> None:
        """Support already-resumable rounds immutably; e.g. source help sends only its Dutch sentence to the translator."""

        catalog = verb_fixtures.catalog(self.db)
        with mock.patch.object(self.db, "verb_catalog", return_value=catalog):
            session = self.engine.start(verb_fixtures.options(1), lambda _: None)
        row = self.db.verb_current(session)
        content = row["content"].copy()
        content.pop("source_english")
        with self.db._connect() as conn:
            conn.execute("UPDATE verb_questions SET body_json=? WHERE id=?", (json.dumps(content), row["question_id"]))
        first = self.engine.english(row["question_id"], lambda _: None)
        again = self.engine.english(row["question_id"], lambda _: None)
        self.assertEqual(first, again)
        self.assertEqual(self.provider.translated, [content["source"]])
        self.assertEqual(self.db.verb_question(row["question_id"]), content)
        self.assertIsNone(self.db.verb_get(row["id"])["answer_text"])

    @mock.patch("yt_whisper_subs.openai_client.responses_api_request")
    def test_real_lexical_provider_requires_independent_strict_audit(self, request) -> None:
        """Verify production contracts without network; e.g. extraction and review remain separate schema-checked requests."""

        rows = [{"lookup_id": 5, "lemma": "fietsen", "part_of_speech": "verb", "context": {"sentence": "Ik fietste."}}]
        item = self.provider.lexemes(rows)[0]
        request.side_effect = [{"output_text": json.dumps({"verbs": [item["lexeme"]]})},
                               {"output_text": json.dumps({"verdicts": [item["verdict"]]})}]
        result = verb_generate.VerbGenerator().lexemes(rows)
        self.assertTrue(result[0]["verdict"]["valid"])
        self.assertEqual(request.call_count, 2)
        for call in request.call_args_list:
            self.assertTrue(call.args[1]["text"]["format"]["strict"])
            self.assertFalse(call.args[1]["store"])

    def test_failed_lexical_audit_repairs_once_then_retries_after_a_day(self) -> None:
        """Bound repeated model work without forgetting unconfirmed words; e.g. an invalid audit waits until tomorrow after repair fails."""

        verb_fixtures.seed_vocab(self.db, [{"surface": "fietste", "lemma": "fietsen"}])
        original = self.provider.lexemes

        def rejected(rows):
            """Keep the audit invalid; e.g. simulate a persistent ambiguity the model cannot resolve."""

            result = original(rows)
            for item in result:
                item["verdict"].update(valid=False, reason="Cannot confirm this contextual mapping")
            return result

        self.provider.lexemes = rejected
        self.sync.sync(lambda _: None)
        self.assertEqual(len(self.provider.lexical_calls), 2)
        self.assertIn("previous_rejection", self.provider.lexical_calls[1][0])
        self.sync.sync(lambda _: None)
        self.assertEqual(len(self.provider.lexical_calls), 2)
        later = time.time() + 86401
        with mock.patch("yt_whisper_subs.verb_catalog_db.time.time", return_value=later):
            self.sync.sync(lambda _: None)
        self.assertEqual(len(self.provider.lexical_calls), 4)
        self.assertFalse(any(row["lemma"] == "fietsen" for row in self.db.verb_catalog()))

    def test_changed_ambiguous_lookup_stops_feeding_new_rounds(self) -> None:
        """Use only currently valid vocabulary links; e.g. a retranslation marked ambiguous cannot keep an orphan verb eligible."""

        ids = verb_fixtures.seed_vocab(self.db, [{"surface": "fietste", "lemma": "fietsen"}])
        self.sync.sync(lambda _: None)
        with self.db._connect() as conn:
            conn.execute("UPDATE vocab_lookups SET ambiguous=1,updated_at=updated_at+1 WHERE id=?", (ids[0],))
        rows = verb_syllabus.candidates(self.db.verb_catalog(), [], verb_fixtures.options())
        self.assertNotIn("fietsen", {row["lemma"] for row in rows})

    def test_mixed_part_of_speech_requires_context_audit_even_for_a_starter(self) -> None:
        """Distinguish homonyms before linking; e.g. noun leven must not bypass review just because the verb is curated."""

        verb_fixtures.seed_vocab(self.db, [{"surface": "leven", "lemma": "leven", "pos": "noun / verb",
                                           "sentence": "Het leven is mooi.", "meaning": "life"}])
        original = self.provider.lexemes

        def noun_reading(rows):
            """Return the known noun reading; e.g. context overrides the broad saved label."""

            items = original(rows)
            for item in items:
                item["lexeme"].update(is_verb=False, lemma="", family=None, transitive=None)
            return items

        self.provider.lexemes = noun_reading
        self.sync.sync(lambda _: None)
        self.assertEqual(len(self.provider.lexical_calls), 1)
        leven = next(row for row in self.db.verb_catalog() if row["lemma"] == "leven")
        self.assertEqual(leven["lookups"], 0)


if __name__ == "__main__":
    unittest.main()
