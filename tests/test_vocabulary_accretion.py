"""Exercise real source-linked discovery, scheduling and paid-work boundaries.

Example: a quiz distractor becomes vocabulary only after it was selected and independently verified.
"""

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests import listening_fixtures
from tests import practice_fixtures
from tests import sentence_fixtures
from tests import test_shared_learning as shared
from yt_whisper_subs import library_db
from yt_whisper_subs import practice
from yt_whisper_subs import practice_activity
from yt_whisper_subs import practice_service
from yt_whisper_subs import practice_stats
from yt_whisper_subs import sentence_practice
from yt_whisper_subs import sentence_service
from yt_whisper_subs import vocabulary_accretion as accretion
from yt_whisper_subs import vocabulary_accretion_worker
from yt_whisper_subs import vocabulary_learning as learning
from yt_whisper_subs import vocabulary_worker


def lexeme(lemma: str, meaning: str) -> learning.Lexeme:
    """Keep fixture dictionaries explicit; e.g. markt has its own meaning market."""

    return learning.Lexeme(lemma=lemma, meaning=meaning, part_of_speech="noun")


class Provider:
    """Supply bounded offline linguistic proposals; e.g. calls reveal whether negative results are reused."""

    def __init__(self) -> None:
        """Allow verification failures without discarding generation; e.g. retry must preserve one proposal call."""

        self.generated, self.verified = [], []
        self.fix = None
        self.splits = []
        self.available = True
        self.accept = True

    def analyse(self, jobs):
        """Return real-word confusions only for selected/typed mistakes; e.g. ordinary dictionary words are negative findings."""

        self.generated.append([row["id"] for row in jobs])
        results = []
        for row in jobs:
            document = row["document"]
            fixes = [self.fix] if self.fix and document["kind"] != "compound" else []
            roots = {word["lemma"] for word in document["lexemes"]}
            for fix in fixes:
                roots.update(word.lemma for word in (fix.intended, fix.confused) if word)
            splits = self.splits if self.splits and self.splits[0].lemma in roots else []
            results.append(accretion.Analysis(id=row["id"], vocabulary=fixes, splits=splits))
        return results

    def verify(self, jobs):
        """Observe separate checking and controlled rejection; e.g. verifier downtime publishes no vocabulary."""

        self.verified.append([row["id"] for row in jobs])
        if not self.available:
            raise ConnectionError("verifier offline")
        return {row["id"]: self.accept for row in jobs}


class AccretionTests(unittest.TestCase):
    """Keep vocabulary eligibility, provenance and reviews consistent across every discovery path."""

    def setUp(self) -> None:
        """Use an empty real catalog and fake provider; e.g. no user vocabulary or API key is read."""

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = library_db.LibraryDb(Path(tmp.name) / "catalog.sqlite3")
        self.db.initialize()
        self.provider = Provider()
        self.worker = vocabulary_accretion_worker.Worker(self.db, "test-owner", self.provider)

    def _drain(self) -> None:
        """Bound recovery so accidental requeue loops fail visibly; e.g. component creation cannot recursively grow paid work."""

        for _ in range(20):
            if not self.worker.step():
                return
        self.fail("Enrichment did not settle")

    def _quiz(self, direction=practice.Direction.EN_NL) -> dict:
        """Choose one genuinely wrong real-word option; e.g. markt versus winkel in either direction."""

        if not self.db.word_summary():
            practice_fixtures.seed(self.db, [("winkel", "shop", "De winkel is open.")])
        engine = practice_service.PracticeService(self.db, practice_fixtures.FakeGenerator())
        opts = practice.RoundOptions(direction=direction, count=1, include_early=True)
        session = engine.start(opts, lambda _: None, cached_first=True)
        row = self.db.practice_current(session)
        text = "markt" if direction == practice.Direction.EN_NL else "market"
        selected = next(idx for idx, item in enumerate(row["choices"]) if item["text"] == text)
        self.provider.fix = learning.Correction(typed=text, dutch=["winkel"], english=["shop"],
                                               intended=lexeme("winkel", "shop"), confused=lexeme("markt", "market"),
                                               note="A market and a shop are different places.")
        return self.db.practice_answer(row["id"], selected, False, 1700)

    def _listening(self, old: str, new: str) -> dict:
        """Grade dictation locally before any dictionary inference; e.g. supporting words are not original targets."""

        sentence_fixtures.seed(self.db)
        engine = sentence_service.SentenceService(self.db, sentence_fixtures.FakeSentenceProvider(),
                                                  speech=listening_fixtures.Speech())
        opts = sentence_fixtures.options(direction=sentence_practice.Direction.LISTEN)
        session = engine.start(opts, lambda _: None)
        row = self.db.sentence_current(session)
        self.db.sentence_submit(row["id"], row["content"]["dutch"].replace(old, new), 1400)
        return engine.check(row["id"], lambda _: None)

    def test_clicked_nested_compound_adds_unassessed_components_with_real_parent_history(self) -> None:
        """Discover reusable words without fake mistakes; e.g. fietsbandventiel adds fietsband, fiets, band and ventiel."""

        practice_fixtures.seed(self.db, [("fietsbandventiel", "bicycle tyre valve", "Het fietsbandventiel lekt.")])
        self.provider.splits = [
            accretion.Split(lemma="fietsbandventiel", parts=[
                accretion.Part(text="fietsband", word=lexeme("fietsband", "bicycle tyre")),
                accretion.Part(text="ventiel", word=lexeme("ventiel", "valve"))]),
            accretion.Split(lemma="fietsband", parts=[
                accretion.Part(text="fiets", word=lexeme("fiets", "bicycle")),
                accretion.Part(text="band", word=lexeme("band", "tyre"))])]
        self._drain()
        words = {word["lemma"]: word for word in self.db.word_candidates("en_nl")}
        self.assertEqual(set(words), {"fietsbandventiel", "fietsband", "fiets", "band", "ventiel"})
        for name in ("fiets", "band", "ventiel", "fietsband"):
            word = words[name]
            self.assertEqual((word["clicks"], word["mistakes"], word["videos"], word["failures"]), (0, 0, 0, 0))
            self.assertIsNone(word["card_json"])
            self.assertEqual(self.db.word_history("nl", name)[0]["origin"], "component")
        self.assertEqual((len(self.provider.generated), len(self.provider.verified)), (1, 1))
        self.assertEqual([split.lemma for split in self.db.word_compounds(words["fietsband"]["lookup_id"])], ["fietsband"])
        self.db.accretion_seed()
        self._drain()
        self.assertEqual(len(self.provider.generated), 1)
        with self.db._connect() as conn:
            self.assertFalse(conn.execute("PRAGMA foreign_key_check").fetchall())

    def test_selected_quiz_confusion_is_imported_once_per_answer_in_both_directions(self) -> None:
        """Keep original quiz failures single and import only the selected distractor; e.g. kantoor is never selected."""

        for direction in practice.Direction:
            with self.subTest(direction=direction):
                self.setUp()
                first = self._quiz(direction)
                self._drain()
                terms = {word["lemma"]: word for word in self.db.word_candidates(direction.value)}
                self.assertEqual(set(terms), {"winkel", "markt"})
                self.assertEqual((terms["winkel"]["failures"], terms["markt"]["failures"]), (1, 1))
                self.assertEqual(terms["markt"]["meaning"], "market")
                calls = len(self.provider.generated), len(self.provider.verified)
                # Limit the next round to the original target while retaining the new actual dictionary word.
                original = [terms["winkel"]]
                with mock.patch.object(self.db, "word_candidates", return_value=original):
                    second = self._quiz(direction)
                self._drain()
                self.assertEqual((len(self.provider.generated), len(self.provider.verified)), calls)
                terms = {word["lemma"]: word for word in self.db.word_candidates(direction.value)}
                self.assertEqual((terms["winkel"]["failures"], terms["markt"]["failures"]), (2, 2))
                self.assertEqual(practice_activity.read(self.db).answers, 2)
                self.assertEqual(practice_stats.snapshot(self.db)["totals"]["attempts"], 4)
                self.db.practice_flag(first["id"])
                if first["question_id"] != second["question_id"]:
                    self.assertEqual(next(row for row in self.db.word_candidates(direction.value) if row["lemma"] == "markt")["failures"], 1)
                    self.db.practice_flag(second["id"])
                self.assertNotIn("markt", {row["lemma"] for row in self.db.word_candidates(direction.value)})
                self.assertTrue(all(row["failures"] == 0 for row in self.db.word_candidates(direction.value)))

    def test_existing_component_keeps_its_dictionary_identity_and_review_progress(self) -> None:
        """Reuse learned constituents without resetting their schedules; e.g. winkel remains the same word inside winkelcentrum."""

        self._quiz()
        self._drain()
        before = next(word for word in self.db.word_candidates("en_nl") if word["lemma"] == "winkel")
        practice_fixtures.seed(self.db, [("winkel", "shop", "De winkel is open."),
                                       ("winkelcentrum", "shopping centre", "Het winkelcentrum is open.")])
        self.provider.splits = [accretion.Split(lemma="winkelcentrum", parts=[
            accretion.Part(text="winkel", word=lexeme("winkel", "shop")),
            accretion.Part(text="centrum", word=lexeme("centrum", "centre"))])]
        self._drain()
        after = next(word for word in self.db.word_candidates("en_nl") if word["lemma"] == "winkel")
        self.assertEqual([before[key] for key in ("word_id", "card_json", "due_at", "attempts", "failures")],
                         [after[key] for key in ("word_id", "card_json", "due_at", "attempts", "failures")])
        self.assertEqual(after["components"], 1)

    def test_listening_imports_legitimate_supporting_word_confusion_after_local_feedback(self) -> None:
        """Add both heard and genuinely written dictionary words; e.g. buurman versus vriend leaves original targets correct."""

        row = self._listening("buurman", "vriend")
        self.assertEqual(row["outcome"], "wrong")
        self.assertNotIn("vriend", {word["lemma"] for word in self.db.word_candidates("nl_en")})
        self.provider.fix = learning.Correction(typed="vriend", dutch=["buurman"], english=["neighbour"],
                                               intended=lexeme("buurman", "neighbour"), confused=lexeme("vriend", "friend"),
                                               note="The script says neighbour; the written word means friend.")
        self._drain()
        terms = {word["lemma"]: word for word in self.db.word_candidates("nl_en")}
        self.assertEqual((terms["buurman"]["failures"], terms["vriend"]["failures"]), (1, 1))
        self.assertEqual((terms["slim"]["failures"], terms["fiets"]["failures"], terms["lopen"]["failures"]), (0, 0, 0))
        self.assertEqual(self.db.word_history("nl", "vriend")[0]["origin"], "listening")
        self.assertEqual(practice_activity.read(self.db).answers, 1)
        self.db.sentence_flag(row["id"])
        self.assertNotIn("vriend", {word["lemma"] for word in self.db.word_candidates("nl_en")})
        with self.db._connect() as conn:
            self.assertIsNone(conn.execute("SELECT * FROM practice_states WHERE word_id=?", (terms["vriend"]["word_id"],)).fetchone())

    def test_listening_target_synonym_is_a_real_transcription_confusion_without_duplicate_failure(self) -> None:
        """Distinguish translation synonyms from the actual spoken wording; e.g. intelligentste was not pronounced slimste."""

        self._listening("slimste", "intelligentste")
        self.provider.fix = learning.Correction(typed="intelligentste", dutch=["slimste"], english=["cleverest"],
                                               intended=lexeme("slim", "clever"), confused=lexeme("intelligent", "intelligent"),
                                               note="Both are real words; only slimste was spoken.")
        self._drain()
        terms = {word["lemma"]: word for word in self.db.word_candidates("nl_en")}
        self.assertEqual((terms["slim"]["failures"], terms["intelligent"]["failures"]), (1, 1))

    def test_listening_confusion_overrides_incidental_success_for_the_same_word(self) -> None:
        """Prefer actual lexical failure to another correct occurrence; e.g. writing fiets for buurman still fails fiets once."""

        self._listening("buurman", "fiets")
        self.provider.fix = learning.Correction(typed="fiets", dutch=["buurman"], english=["neighbour"],
                                               intended=lexeme("buurman", "neighbour"), confused=lexeme("fiets", "bicycle"),
                                               note="Fiets was substituted for neighbour, despite another correct bicycles occurrence.")
        self._drain()
        word = next(word for word in self.db.word_candidates("nl_en") if word["lemma"] == "fiets")
        self.assertEqual((word["attempts"], word["failures"]), (1, 1))

    def test_imported_compound_reuses_embedded_splits_without_duplicate_component_events(self) -> None:
        """Use one combined request for new words and their components; e.g. buurman adds buur/man once each."""

        row = self._listening("buurman", "vriend")
        self.provider.fix = learning.Correction(typed="vriend", dutch=["buurman"], english=["neighbour"],
                                               intended=lexeme("buurman", "neighbour"), confused=lexeme("vriend", "friend"),
                                               note="The script says neighbour; the written word means friend.")
        self.provider.splits = [accretion.Split(lemma="buurman", parts=[
            accretion.Part(text="buur", word=lexeme("buur", "neighbour")),
            accretion.Part(text="man", word=lexeme("man", "man"))])]
        self._drain()
        self.assertEqual((len(self.provider.generated), len(self.provider.verified)), (1, 1))
        history = self.db.word_history("nl", "buur")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["title"], "Component of buurman")
        self.db.sentence_flag(row["id"])
        self.assertNotIn("buur", {word["lemma"] for word in self.db.word_candidates("nl_en")})

    def test_background_service_backfills_old_failures_without_another_click(self) -> None:
        """Resume after an upgrade without opening practice; e.g. previously saved quiz mistakes enrich in the library."""

        from tests import test_vocabulary

        self._quiz()
        with self.db._connect() as conn:
            conn.execute("DELETE FROM vocab_audit_sources")
            conn.execute("DELETE FROM vocab_audits")
        service = vocabulary_worker.VocabularyService(self.db, auditor=self.provider)
        try:
            service.resume()
            test_vocabulary.wait_for(lambda: "markt" in {word["lemma"] for word in self.db.word_candidates("en_nl")})
        finally:
            service.close()
            service._threads[2].join(3)
        self.assertEqual(practice_activity.read(self.db).answers, 1)

    def test_source_reported_during_verification_cannot_publish_or_starve_other_work(self) -> None:
        """Check source validity again on publication; e.g. a saved paid result remains reusable but inactive after reporting."""

        row = self._quiz()
        self.worker.step()
        jobs = self.db.accretion_claim("verifier")
        self.db.practice_flag(row["id"])
        self.db.accretion_verified("verifier", self.provider.verify(jobs))
        self._drain()
        self.assertNotIn("markt", {word["lemma"] for word in self.db.word_candidates("en_nl")})
        practice_fixtures.seed(self.db, [("winkel", "shop", "De winkel is open."), ("anders", "different", "Het is anders.")])
        self.assertGreater(self.worker.step(), 0)

    def test_rejected_and_empty_findings_are_cached_without_publishing_words(self) -> None:
        """Avoid repeated paid attempts after a conservative rejection; e.g. a verifier veto remains a negative finding."""

        self._quiz()
        self.provider.accept = False
        self._drain()
        self.assertNotIn("markt", {word["lemma"] for word in self.db.word_candidates("en_nl")})
        calls = len(self.provider.generated), len(self.provider.verified)
        self.db.accretion_seed()
        self._drain()
        self.assertEqual((len(self.provider.generated), len(self.provider.verified)), calls)

    def test_verification_retry_reuses_proposal_and_reporting_during_http_prevents_import(self) -> None:
        """Preserve work across outage and source invalidation; e.g. reported market confusion cannot publish later."""

        row = self._quiz()
        self.worker.step()
        self.provider.available = False
        self.worker.step()
        self.assertIn("failed", self.db.accretion_status())
        self.assertNotIn("markt", {word["lemma"] for word in self.db.word_candidates("en_nl")})
        generated = len(self.provider.generated)
        self.db.accretion_retry()
        self.provider.available = True
        self._drain()
        self.assertEqual(len(self.provider.generated), generated)
        self.assertIn("markt", {word["lemma"] for word in self.db.word_candidates("en_nl")})
        self.db.practice_flag(row["id"])
        self.db.accretion_seed()
        self._drain()
        self.assertNotIn("markt", {word["lemma"] for word in self.db.word_candidates("en_nl")})

    def test_stale_lease_is_recovered_but_old_completion_cannot_publish(self) -> None:
        """Exclude simultaneous workers and protect lease replacement; e.g. an old negative finding cannot erase a valid proposal."""

        self._quiz()
        jobs = self.db.accretion_claim("old")
        self.assertTrue(jobs)
        self.assertFalse(self.db.accretion_claim("new"))
        with self.db._connect() as conn:
            conn.execute("UPDATE vocab_audits SET lease_until=0")
        reclaimed = self.db.accretion_claim("new")
        self.db.accretion_proposed("old", self.provider.analyse(jobs))
        self.assertEqual(self.db.accretion_status(), {"working": len(jobs)})
        self.db.accretion_proposed("new", self.provider.analyse(reclaimed))
        self._drain()
        self.assertIn("markt", {word["lemma"] for word in self.db.word_candidates("en_nl")})

    def test_sentence_imports_share_compound_queue_and_cached_backfill(self) -> None:
        """Cover the existing intended/confused importer too; e.g. veilig and zeker each receive one reusable morphology audit."""

        case = shared.SharedLearningTests(methodName="runTest")
        case.setUp()
        self.addCleanup(case.doCleanups)
        case._wrong()
        worker = vocabulary_accretion_worker.Worker(case.db, "shared", self.provider)
        while worker.step():
            pass
        with case.db._connect() as conn:
            lemmas = {json.loads(row[0])["lexemes"][0]["lemma"] for row in conn.execute("SELECT document_json FROM vocab_audits")}
        self.assertTrue({"veilig", "zeker"} <= lemmas)
        calls = len(self.provider.generated)
        case.db.accretion_seed()
        self.assertEqual(worker.step(), 0)
        self.assertEqual(len(self.provider.generated), calls)

    def test_invalid_decompositions_and_ungrounded_confusions_are_rejected(self) -> None:
        """Enforce structural evidence independently of LLM approval; e.g. no orphan split or unexplained letters."""

        document = dict(kind="compound", lexemes=[lexeme("kinderwagen", "pram").model_dump()])
        parts = [accretion.Part(text="kind", word=lexeme("kind", "child")), accretion.Part(text="wagen", word=lexeme("wagen", "cart"))]
        bad = accretion.Analysis(id=1, vocabulary=[], splits=[accretion.Split(lemma="kinderwagen", parts=parts)])
        with self.assertRaisesRegex(ValueError, "reconstruct"):
            bad.check(document)
        good = bad.model_copy(deep=True)
        good.splits[0].parts.insert(1, accretion.Part(text="er", word=None))
        good.check(document)
        good.splits[0].lemma = "anders"
        with self.assertRaisesRegex(ValueError, "disconnected"):
            good.check(document)

    def test_one_invalid_proposal_preserves_other_batch_results_and_focused_repair(self) -> None:
        """Checkpoint per item before repair; e.g. one broken winkel split does not purchase anders twice."""

        practice_fixtures.seed(self.db, [("winkel", "shop", "De winkel is open."), ("anders", "different", "Het is anders.")])
        jobs = self.db.accretion_claim("batch")
        proposals = self.provider.analyse(jobs)
        bad = next(item for item in proposals if item.id == jobs[0]["id"])
        bad.splits = [accretion.Split(lemma="winkel", parts=[
            accretion.Part(text="win", word=lexeme("win", "profit")),
            accretion.Part(text="kl", word=lexeme("kl", "invalid"))])]
        self.db.accretion_proposed("batch", proposals)
        self.assertEqual(self.db.accretion_status(), {"done": 1, "failed": 1})
        self.db.accretion_retry()
        repairs = self.db.accretion_claim("repair")
        self.assertEqual([row["id"] for row in repairs], [bad.id])
        self.assertIn("previous_proposal", repairs[0]["repair"])
        self.db.accretion_proposed("repair", [accretion.Analysis(id=bad.id, vocabulary=[], splits=[])])
        self._drain()
        self.assertEqual(self.db.accretion_status(), {"done": 2})

    def test_cleanup_archives_sources_and_preserves_answers_without_reimport(self) -> None:
        """Remove reviewed errors from active vocabulary only; e.g. a saved quiz answer remains in activity history."""

        row = self._quiz()
        removed = self.db.remove_vocabulary([dict(language="nl", lemma="winkel", reason="Reviewed synthetic incorrect source word.",
                                                   evidence=dict(context="Synthetic cleanup fixture"))])
        self.assertEqual(removed, ["winkel"])
        self._drain()
        self.assertNotIn("winkel", {word["lemma"] for word in self.db.word_candidates("en_nl")})
        self.assertEqual(practice_activity.read(self.db).answers, 1)
        with self.db._connect() as conn:
            archive = conn.execute("SELECT archived_json FROM vocab_exclusions WHERE lemma_norm='winkel'").fetchone()[0]
            self.assertTrue(json.loads(archive)["vocab_clicks"])
            self.assertEqual(conn.execute("SELECT outcome FROM practice_presentations WHERE id=?", (row["id"],)).fetchone()[0], "wrong")
            self.assertFalse(conn.execute("PRAGMA foreign_key_check").fetchall())

    def test_cleanup_can_independently_retain_valid_constituents(self) -> None:
        """Avoid losing good vocabulary when its parent was bogus; e.g. verified fiets and band remain unassessed."""

        self.test_clicked_nested_compound_adds_unassessed_components_with_real_parent_history()
        self.db.remove_vocabulary([dict(language="nl", lemma="fietsbandventiel", reason="Synthetic invalid parent for cleanup verification.",
                                       evidence=dict(context="Synthetic fixture"), keep_components=["fietsband", "fiets", "band", "ventiel"])])
        words = {word["lemma"]: word for word in self.db.word_candidates("en_nl")}
        self.assertEqual(set(words), {"fietsband", "fiets", "band", "ventiel"})
        self.assertTrue(all(word["failures"] == 0 for word in words.values()))

    def test_spelling_correction_restores_quarantine_and_preserves_progress(self) -> None:
        """Correct a clear typo instead of losing its history; e.g. eindordeel becomes eindoordeel with an exact updated span."""

        practice_fixtures.seed(self.db, [("eindordeel", "final judgment", "Dat is het eindordeel.")])
        word = self.db.word_candidates("nl_en")[0]
        policy = practice.ReviewPolicy()
        result = policy.answer(None, correct=False, unsure=False, now=time.time(), elapsed_ms=1000)
        with self.db._connect() as conn:
            self.db._practice_state(conn, dict(word_id=word["word_id"], direction="nl_en"), result)
        self.db.remove_vocabulary([dict(language="nl", lemma="eindordeel", reason="Clearly misspelled final-judgment compound.",
                                       evidence=dict(sentence="Dat is het eindordeel."))])
        fixed = self.db.repair_vocabulary([dict(language="nl", old="eindordeel", new="eindoordeel", reason="Established compound spelling.",
                                                surfaces={"eindordeel": "eindoordeel"})])
        after = self.db.word_candidates("nl_en")[0]
        self.assertEqual((after["word_id"], after["surface"], after["lemma"], after["clicks"]),
                         (word["word_id"], "eindoordeel", "eindoordeel", 1))
        self.assertEqual(after["card_json"], result["after_json"])
        context = json.loads(after["context_json"])
        start, end = context["span"]
        self.assertEqual(context["sentence"][start:end], "eindoordeel")
        self.assertEqual(fixed[0]["new"], "eindoordeel")
        with self.db._connect() as conn:
            self.assertFalse(conn.execute("PRAGMA foreign_key_check").fetchall())
