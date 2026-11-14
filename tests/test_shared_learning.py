"""Verify communicating vocabulary, sentence and verb learning with actual SQLite and FSRS.

Example: zekerder for veiliger adds both correctly translated concepts and shared failed reviews, without fake video clicks.
"""

import os
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6 import QtCore
from PySide6 import QtWidgets

from tests import sentence_fixtures
from tests import verb_fixtures
from yt_whisper_subs import library_db
from yt_whisper_subs import library_vocabulary
from yt_whisper_subs import practice
from yt_whisper_subs import practice_activity
from yt_whisper_subs import practice_stats
from yt_whisper_subs import sentence_practice as sentences
from yt_whisper_subs import sentence_select
from yt_whisper_subs import sentence_service
from yt_whisper_subs import verb_service
from yt_whisper_subs import verb_practice as verbs
from yt_whisper_subs import verb_syllabus
from yt_whisper_subs import vocabulary_learning as learning
from yt_whisper_subs import word_select


DUTCH = "De slimste buurman zou naar de winkel lopen omdat zijn fietsen daar veiliger staan."
ENGLISH = "The cleverest neighbour would walk to the shop because his bicycles are safer there."


class SharedLearningTests(unittest.TestCase):
    """Keep source evidence, scheduling and activity consistent; e.g. one sentence can reveal two confused dictionary words."""

    @classmethod
    def setUpClass(cls) -> None:
        """Share native vocabulary rendering; e.g. practice-only entries cannot offer video playback."""

        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self) -> None:
        """Create genuine saved targets and a replaceable provider; e.g. the new supporting adjective is initially absent."""

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = library_db.LibraryDb(Path(tmp.name) / "catalog.sqlite3")
        self.db.initialize()
        sentence_fixtures.seed(self.db)
        self.provider = sentence_fixtures.FakeSentenceProvider()
        generate = self.provider.generate

        def natural(*args):
            """Add a supporting adjective outside the target list; e.g. saved bicycles need not restrict surrounding vocabulary."""

            items = generate(*args)
            for item in items:
                item["draft"].update(dutch=DUTCH, english=ENGLISH)
            return items

        self.provider.generate = natural
        self.engine = sentence_service.SentenceService(self.db, self.provider)

    @staticmethod
    def _fix(*, reverse=False) -> learning.Correction:
        """Describe both real lexical identities; e.g. certain stays the meaning of zeker instead of acquiring safe."""

        return learning.Correction(typed="more certain" if reverse else "zekerder", dutch=["veiliger"], english=["safer"],
                                   intended=learning.Lexeme(lemma="veilig", meaning="safe", part_of_speech="adjective"),
                                   confused=learning.Lexeme(lemma="zeker", meaning="certain", part_of_speech="adjective"),
                                   note="Zeker means certain; veilig means safe. Their comparatives express different concepts.")

    def _wrong(self, *, reverse=False, capture=True):
        """Submit a real graded supporting-word confusion; e.g. all three original saved targets can still be correct."""

        direction = practice.Direction.NL_EN if reverse else practice.Direction.EN_NL
        original = [word for word in self.db.word_candidates(direction.value) if word["lemma"] in {"slim","fiets","lopen"}]
        with mock.patch.object(self.db,"word_candidates",return_value=original):
            session = self.engine.start(sentence_fixtures.options(direction=direction, include_early=True), lambda _: None)
        row = self.db.sentence_current(session)
        answer = ENGLISH.replace("safer", "more certain") if reverse else DUTCH.replace("veiliger", "zekerder")
        words = [sentences.WordResult(word_id=word["word_id"], meaning_correct=True,target_used=True,form_correct=True,
                                       explanation="Saved target used correctly.",
                                       forms=[sentences.FormResult(feature=key,correct=True,explanation="Correct form.") for key in word["features"]])
                 for word in row["content"]["words"]]
        grade = sentences.Grade(confident=True,correct=False,corrected=ENGLISH if reverse else DUTCH,
                                 corrected_meaning=DUTCH if reverse else ENGLISH,
                                 explanation="Use the concept safe rather than certain.", words=words,
                                 errors=[sentences.Error(kind="meaning",word_ids=[],evidence="more certain" if reverse else "zekerder",
                                                          correction="safer" if reverse else "veiliger", explanation="Safe and certain differ.")],
                                 vocabulary=[self._fix(reverse=reverse)] if capture else [])
        self.provider.grade_result = grade
        self.db.sentence_submit(row["id"], answer, 2400)
        return self.engine.check(row["id"], lambda _: None)

    def test_both_concepts_enter_vocabulary_and_receive_same_again_as_wrong_word_choice(self) -> None:
        """Transfer the exact failed lexical review policy; e.g. safe and certain become due in English-to-Dutch word practice."""

        row = self._wrong()
        terms = {word["surface"]: word for word in self.db.word_candidates("en_nl")}
        for lemma, meaning in (("veilig", "safe"), ("zeker", "certain")):
            word = terms[lemma]
            self.assertEqual((word["meaning"],word["clicks"],word["videos"],word["mistakes"],word["failures"]), (meaning,0,0,1,1))
            policy = practice.ReviewPolicy(row["scheduler_json"])
            card = policy.new_card(word["word_id"] * 2 + 1, row["answered_at"])
            expected = policy.answer(card,correct=False,unsure=False,now=row["answered_at"],elapsed_ms=2400)
            self.assertEqual(word["card_json"], expected["after_json"])
        self.assertEqual(practice_activity.read(self.db).answers, 1)
        stats = {term["word"]: term for term in practice_stats.snapshot(self.db)["terms"]}
        self.assertEqual((stats["veilig"]["wrong"],stats["zeker"]["wrong"]), (1,1))
        self.assertEqual(self.db.sentence_get(row["id"])["grade"]["words"][0]["meaning_correct"], True)

    def test_shared_selectors_have_identical_candidates_probabilities_and_due_rules(self) -> None:
        """Use one algorithm rather than approximate priors; e.g. the same seed chooses the same due words in either mode."""

        row = self._wrong()
        words = self.db.word_candidates("en_nl")
        now = row["answered_at"] + 61
        policy = practice.ReviewPolicy()
        quiz = word_select.Selector(policy,np.random.default_rng(7))
        sentence = sentence_select.Selector(policy,np.random.default_rng(7))
        np.testing.assert_equal(quiz.weights(words,now), sentence.weights(words,now))
        opts = practice.RoundOptions(direction=practice.Direction.EN_NL,count=2)
        a,b = quiz.select(words,opts,now),sentence.select(words,sentences.Options(count=2),now)
        self.assertEqual([word["word_id"] for word in a], [word["word_id"] for word in b])
        self.assertEqual({word["surface"] for word in a}, {"veilig","zeker"})

    def test_repeated_mistakes_reuse_dictionary_entries_and_preserve_every_event(self) -> None:
        """Keep countable repeated evidence without duplicate lexemes; e.g. two attempts produce two veilig/certain confusions."""

        self._wrong()
        self._wrong()
        words = {word["surface"]: word for word in self.db.word_summary()}
        self.assertEqual((words["veilig"]["mistakes"],words["zeker"]["mistakes"]),(2,2))
        with self.db._connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM vocab_words").fetchone()[0],5)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM vocab_clicks").fetchone()[0],3)
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(),[])

    def test_idempotency_and_reporting_remove_all_influence_without_erasing_evidence(self) -> None:
        """Reverse disputed learning throughout the shared state; e.g. a reported correction cannot keep generating vocabulary failures."""

        row = self._wrong()
        self.engine.check(row["id"],lambda _:None)
        self.db.sentence_flag(row["id"])
        self.assertNotIn("veilig",{word["surface"] for word in self.db.word_candidates("en_nl")})
        self.assertEqual(practice_stats.snapshot(self.db)["totals"]["attempts"],0)
        self.assertEqual(practice_activity.read(self.db).answers,1)
        with self.db._connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM vocab_errors").fetchone()[0],1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM word_transfers").fetchone()[0],5)

    def test_reverse_direction_keeps_correct_dutch_lexemes_and_directional_review(self) -> None:
        """Learn a confused English concept's Dutch counterpart; e.g. more certain for veiliger reviews zeker and veilig in comprehension."""

        self._wrong(reverse=True)
        terms = {word["surface"]:word for word in self.db.word_candidates("nl_en")}
        self.assertEqual(terms["zeker"]["meaning"],"certain")
        self.assertIsNotNone(terms["veilig"]["card_json"])
        reverse = {word["surface"]:word for word in self.db.word_candidates("en_nl")}
        self.assertIsNone(reverse["veilig"]["card_json"])

    def test_untrusted_capture_rolls_back_grade_and_does_not_add_vocabulary(self) -> None:
        """Require grounded bilingual evidence; e.g. an absent intended form leaves the original answer pending."""

        fix = self._fix()
        fix.dutch = ["not in this Dutch sentence"]
        with mock.patch.object(self,"_fix",return_value=fix):
            with self.assertRaisesRegex(ValueError,"absent"):
                self._wrong()
        with self.db._connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM vocab_enrichments").fetchone()[0],0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM word_transfers").fetchone()[0],0)
            self.assertEqual(conn.execute("SELECT outcome FROM sentence_presentations").fetchone()[0],None)

    def test_native_vocabulary_displays_mistakes_without_playback_or_fake_clicks(self) -> None:
        """Expose the new source in the normal vocabulary UI; e.g. a mistake-only word has zero videos and no Play action."""

        self._wrong()
        service = SimpleNamespace(db=self.db,vocabulary=SimpleNamespace(start=lambda:None))
        dialog = library_vocabulary.VocabularyDialog(service)
        self.addCleanup(dialog.close)
        dialog._search.setText("veilig")
        self.assertEqual(dialog._words.rowCount(),1)
        dialog._words.selectRow(0)
        self.app.processEvents()
        self.assertFalse(dialog._play.isEnabled())
        self.assertIn("zekerder",dialog._detail.toPlainText())
        self.assertEqual(dialog._words.item(0,4).data(QtCore.Qt.ItemDataRole.DisplayRole),0)
        self.assertEqual(dialog._words.item(0,5).data(QtCore.Qt.ItemDataRole.DisplayRole),1)

    def _quiz(self, word: dict, correct: bool, at: float) -> dict:
        """Use a real quiz presentation for subsequent review; e.g. reporting it must retain earlier transferred failures."""

        policy = practice.ReviewPolicy()
        body = {"surface":word["surface"],"prompt":word["meaning"],"answer":word["surface"],"context":"",
                "source_sentence":"", "meaning":word["meaning"],"explanation":"Controlled word question.","distractors":[]}
        question = self.db.practice_cache({"lookup_id":word["lookup_id"],"direction":"en_nl","body_json":json.dumps(body),
                                          "validation_json":json.dumps({"valid":True}),"model":"test","reasoning":"low","prompt_version":1})
        choices = [{"text":word["surface"],"explanation":"Correct","kind":"correct"},
                   {"text":"another concept","explanation":"Wrong","kind":"other"}]
        opts = practice.RoundOptions(direction=practice.Direction.EN_NL,count=1,include_early=True)
        with mock.patch("yt_whisper_subs.practice_db.time.time",return_value=at):
            session = self.db.practice_create(opts.model_dump(mode="json"),[{"id":question["id"],"choices":choices,"correct_idx":0}],policy.document())
            row = self.db.practice_current(session)
            return self.db.practice_answer(row["id"],0 if correct else 1,False,1500)

    def test_quiz_success_updates_sentence_eligibility_and_reporting_replays_both_modes(self) -> None:
        """Share subsequent success and loss of disputed evidence; e.g. reporting a quiz success restores the earlier sentence Again state."""

        row = self._wrong()
        word = next(word for word in self.db.word_candidates("en_nl") if word["lemma"]=="veilig")
        first = word["card_json"]
        quiz = self._quiz(word,True,row["answered_at"]+70)
        updated = next(word for word in self.db.word_candidates("en_nl") if word["lemma"]=="veilig")
        self.assertNotEqual(updated["card_json"],first)
        selector = sentence_select.Selector(practice.ReviewPolicy())
        selected = selector.select([updated],sentences.Options(),row["answered_at"]+71)
        self.assertEqual(selected,[])
        self.db.practice_flag(quiz["id"])
        restored = next(word for word in self.db.word_candidates("en_nl") if word["lemma"]=="veilig")
        self.assertEqual(restored["card_json"],first)

    def test_existing_inflected_entry_is_reused_and_unknown_confused_word_is_not_invented(self) -> None:
        """Deduplicate by known dictionary form; e.g. captured veiliger and corrected veilig remain one vocabulary identity."""

        saved = verb_fixtures.seed_vocab(self.db,[{"surface":"veiliger","lemma":"veilig","pos":"adjective",
                                                  "meaning":"safer","sentence":"Dit pad is veiliger.","event":"existing-safe"}])[0]
        with self.db._connect() as conn:
            original = conn.execute("SELECT word_id FROM vocab_lookups WHERE id=?",(saved,)).fetchone()[0]
        fix = self._fix()
        fix.confused = None
        with mock.patch.object(self,"_fix",return_value=fix):
            self._wrong()
        words = self.db.word_candidates("en_nl")
        intended = next(word for word in words if word["lemma"]=="veilig")
        self.assertEqual(intended["word_id"],original)
        self.assertNotIn("zeker",{word["lemma"] for word in words})

    def test_word_quiz_failure_increases_verb_lexeme_priority(self) -> None:
        """Carry word difficulty into grammar selection; e.g. forgetting werken boosts its transformations while maken is unaffected."""

        verb_fixtures.seed_vocab(self.db,[{"surface":"werkt","lemma":"werken","pos":"verb","meaning":"works",
                                         "sentence":"Hij werkt hier.","event":"shared-work"}])
        opts = verb_fixtures.options().model_copy(update={"source":"starter"})
        catalog = [verb for verb in self.db.verb_catalog() if verb["lemma"] in {"werken","maken"}]
        rows = verb_syllabus.candidates(catalog,[],opts)
        selector,now = verb_syllabus.Selector(practice.ReviewPolicy()),time.time()
        before = dict(zip([row["lemma"] for row in rows],selector.weights(rows,[],now)))
        word = next(word for word in self.db.word_candidates("en_nl") if word["lemma"]=="werken")
        self._quiz(word,False,now)
        catalog = [verb for verb in self.db.verb_catalog() if verb["lemma"] in {"werken","maken"}]
        rows = verb_syllabus.candidates(catalog,[],opts)
        after = dict(zip([row["lemma"] for row in rows],selector.weights(rows,[],now)))
        self.assertGreater(after["werken"],before["werken"])
        self.assertEqual(after["maken"],before["maken"])

    def test_verb_lexical_mistake_adds_both_concepts_to_shared_word_review(self) -> None:
        """Connect lexical mistakes from transformations too; e.g. wrong certainty in a Dutch rewrite adds safe and certain with source verb practice."""

        provider = verb_fixtures.FakeVerbProvider()
        generate = provider.generate

        def exercise(targets):
            """Supply a valid safety-context transformation; e.g. only subject and time change before the learner's lexical substitution."""

            items = generate(targets)
            for item in items:
                item["draft"].update(source="Ik werkte gisteren veiliger.",source_english="I worked more safely yesterday.",
                                     answer="Jij werkt vandaag veiliger.",answer_english="You work more safely today.",alternatives=[])
            return items

        provider.generate = exercise
        catalog = [verb for verb in self.db.verb_catalog() if verb["lemma"]=="werken"]
        engine = verb_service.VerbService(self.db,provider)
        with mock.patch.object(self.db,"verb_catalog",return_value=catalog):
            session = engine.start(verb_fixtures.options(1).model_copy(update={"source":"starter"}),lambda _:None)
        row = self.db.verb_current(session)
        fix = self._fix().model_copy(update={"english":["more safely"]})
        provider.grade_result = verbs.Grade(confident=True,correct=False,corrected="Jij werkt vandaag veiliger.",
                                             corrected_english="You work more safely today.", explanation="Certain and safe differ.",
                                             axes=[verbs.Axis(axis=key,correct=key!="verb") for key in verbs.AXES],
                                             errors=[verbs.Error(kind="meaning",axis="verb",evidence="zekerder",correction="veiliger",
                                                                explanation="The intended word expresses safety.")],vocabulary=[fix])
        self.db.verb_submit(row["id"],"Jij werkt vandaag zekerder.",1600)
        engine.check(row["id"],lambda _:None)
        self.assertEqual({word["lemma"] for word in self.db.word_candidates("en_nl") if word["failures"]}, {"veilig","zeker"})
        self.assertEqual(practice_activity.read(self.db).answers,1)
        self.db.verb_flag(row["id"])
        self.assertNotIn("veilig",{word["lemma"] for word in self.db.word_candidates("en_nl")})

    @mock.patch("yt_whisper_subs.openai_client.responses_api_request")
    def test_focused_metadata_repair_preserves_the_existing_grade(self, request) -> None:
        """Repair only a malformed lexical annotation; e.g. exact word/grammar judgments survive a missing-evidence retry."""

        from yt_whisper_subs import sentence_generate

        row = self._wrong(capture=False)
        grade = sentences.Grade.model_validate(row["grade"])
        bad = self._fix().model_copy(update={"english":["not in the source"]})
        grade.vocabulary = [bad]
        repaired = learning.Corrections(vocabulary=[self._fix()])
        request.side_effect = [{"output_text":grade.model_dump_json()}, {"output_text":repaired.model_dump_json()}]
        document = {"direction":row["direction"],"exercise":row["content"],"learner_answer":row["answer_text"]}
        result = sentence_generate.SentenceGenerator().grade(document)
        self.assertEqual(result.errors,grade.errors)
        self.assertEqual(result.words,grade.words)
        self.assertEqual(result.vocabulary,repaired.vocabulary)
        names = [call.args[1]["text"]["format"]["name"] for call in request.call_args_list]
        self.assertEqual(names,["grade","corrections"])

    def test_due_failed_word_is_not_hidden_by_cached_new_questions(self) -> None:
        """Keep caching subordinate to spaced review; e.g. newly discovered veilig cannot be starved by a stock of easy unseen words."""

        now = time.time()
        policy = practice.ReviewPolicy()
        result = policy.answer(None,correct=False,unsure=False,now=now-1000,elapsed_ms=0)
        rows = [{"word_id":1,"card_json":result["after_json"],"due_at":result["due_at"],"unused":False},
                {"word_id":2,"card_json":None,"due_at":None,"unused":True}]
        opts = practice.RoundOptions(direction=practice.Direction.EN_NL,count=1)
        self.assertEqual(word_select.Selector(policy).select(rows,opts,now)[0]["word_id"],1)
