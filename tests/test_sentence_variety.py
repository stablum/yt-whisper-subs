"""Check vocabulary breadth, bounded sentence replay and example privacy with real stored rounds.

Example: ten due whole sentences and twenty unused words must not produce another eight-replay round.
"""

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests import listening_fixtures
from tests import sentence_fixtures
from tests import verb_fixtures
from yt_whisper_subs import library_db
from yt_whisper_subs import practice
from yt_whisper_subs import sentence_generate
from yt_whisper_subs import sentence_practice as sentences
from yt_whisper_subs import sentence_select
from yt_whisper_subs import sentence_service


class SentenceVarietyTests(unittest.TestCase):
    """Exercise adaptive selection and checked caches together; e.g. a replay cap cannot fabricate short rounds or erase past outcomes."""

    def test_standby_does_not_take_an_active_due_sentence_in_a_small_pool(self):
        """Keep an active review out of the next round even when target diversity is limited; e.g. the exact-review quota cannot bypass reservation."""

        candidates = self.db.word_candidates("en_nl")[:13]
        with mock.patch.object(self.db, "word_candidates", return_value=candidates):
            session = self.engine.start(self.opts, lambda _: None)
            row = self.db.sentence_current(session)
            self.db.sentence_submit(row["id"], row["content"]["dutch"], 1000)
            self.engine.check(row["id"], lambda _: None)
            with self.db._connect() as conn:
                conn.execute("UPDATE sentence_question_states SET due_at=?", (time.time() - 1,))
                active = {item[0] for item in conn.execute("SELECT question_id FROM sentence_presentations WHERE session_id=?", (session,))}
            self.engine.start(self.opts, lambda _: None, background=True)
        with self.db._connect() as conn:
            spare = {item[0] for item in conn.execute("SELECT question_id FROM sentence_stock_questions")}
        self.assertEqual(len(spare), 10)
        self.assertFalse(active & spare)
        self.assertEqual(self.db.sentence_active("en_nl"), session)

    def setUp(self):
        """Seed a pool larger than a round and inject checked content; e.g. tests use no learner catalog or API key."""

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = library_db.LibraryDb(Path(folder.name) / "catalog.sqlite3")
        self.db.initialize()
        entries = [{"surface": f"woord{i}", "lemma": f"woord{i}", "pos": "noun", "meaning": f"term {i}"} for i in range(30)]
        verb_fixtures.seed_vocab(self.db, entries)
        self.provider = sentence_fixtures.SimpleSentenceProvider()
        self.provider.generate = mock.Mock(wraps=self.provider.generate)
        self.engine = sentence_service.SentenceService(self.db, self.provider)
        self.opts = sentences.Options(count=10, words=1, scene="home")

    def _past_round(self):
        """Create actual reviewed history and due writing targets; e.g. whole-sentence reviews may be due while vocabulary meanings are mastered."""

        session = self.engine.start(self.opts, lambda _: None)
        rows = []
        for item in self.db.sentence_history(session):
            row = self.db.sentence_open(item["id"])
            self.db.sentence_submit(row["id"], row["content"]["dutch"], 1500)
            rows.append(self.engine.check(row["id"], lambda _: None))
        with self.db._connect() as conn:
            conn.execute("UPDATE sentence_question_states SET due_at=?", (time.time() - 1,))
        self.provider.generate.reset_mock()
        return session, rows

    def test_due_sentence_replays_take_two_of_ten_when_other_targets_exist(self):
        """Preserve adaptive review without a repetition-heavy round; e.g. eight other targets consume eight new drafts rather than repeating eight questions."""

        previous, rows = self._past_round()
        old = {row["question_id"] for row in rows}
        session = self.engine.start(self.opts, lambda _: None)
        following = [self.db.sentence_get(item["id"]) for item in self.db.sentence_history(session)]
        self.assertEqual(len(following), 10)
        self.assertEqual(sum(row["question_id"] in old for row in following), 2)
        self.assertEqual(len({row["anchor_word_id"] for row in following}), 10)
        drafts = sum(len(call.args[0]) for call in self.provider.generate.call_args_list)
        self.assertEqual(drafts, 8)
        self.assertEqual(self.db.sentence_summary(previous)["correct"], 10)

    def test_small_pool_can_fill_with_due_reviews_without_unnecessary_generation(self):
        """Relax variety only when no other eligible anchors exist; e.g. a small mastered vocabulary still gets a full due grammar round."""

        _, rows = self._past_round()
        ids = {row["anchor_word_id"] for row in rows}
        candidates = [row for row in self.db.word_candidates("en_nl") if row["word_id"] in ids]
        with mock.patch.object(self.db, "word_candidates", return_value=candidates):
            session = self.engine.start(self.opts, lambda _: None)
        self.assertEqual(self.db.sentence_summary(session)["planned"], 10)
        self.provider.generate.assert_not_called()

    def test_used_cached_sentences_cannot_bypass_the_replay_quota(self):
        """Keep the quota honest even with one-word fixed-context plans; e.g. eight fresh slots cannot silently reuse old cached variants."""

        _, rows = self._past_round()
        old = {row["question_id"] for row in rows}
        ids = {row["anchor_word_id"] for row in rows}
        with self.db._connect() as conn:
            conn.execute("UPDATE practice_states SET due_at=?", (time.time() - 1,))
        candidates = [row for row in self.db.word_candidates("en_nl") if row["word_id"] in ids]
        with mock.patch.object(self.db, "word_candidates", return_value=candidates):
            session = self.engine.start(self.opts, lambda _: None, cached_first=True)
        following = [self.db.sentence_get(item["id"]) for item in self.db.sentence_history(session)]
        self.assertEqual(sum(row["question_id"] in old for row in following), 2)
        self.assertEqual(sum(len(call.args[0]) for call in self.provider.generate.call_args_list), 8)
        self.assertTrue(all(call.args[0][0]["recent_sentences"] for call in self.provider.generate.call_args_list))

    def test_exact_reviews_honor_target_word_count(self):
        """Keep review stock within selected filters; e.g. a three-target review cannot bypass a one-word-per-sentence setting."""

        self._past_round()
        generation = {"direction": "en_nl", "model": self.provider.model, "reasoning": self.provider.reasoning,
                      "prompt_version": sentence_generate.PROMPT_VERSION}
        reviews = self.db.sentence_due(generation, time.time())
        for row in reviews:
            row["word_count"] = 3
        selector = sentence_select.Selector(practice.ReviewPolicy())
        checked, selected = selector.targets(self.db.word_candidates("en_nl"), reviews, self.opts, time.time())
        self.assertEqual(checked, [])
        self.assertEqual(len(selected), 10)

    def test_recent_examples_are_bounded_and_exclude_answers_and_unused_stock(self):
        """Send only previously shown reference pairs to generation; e.g. private typed answers never become novelty examples."""

        _, rows = self._past_round()
        anchor = next(row for row in self.db.word_candidates("en_nl") if row["word_id"] == rows[0]["anchor_word_id"])
        opts = self.opts.model_copy(update={"count": 1, "include_early": True})
        selector = sentence_select.Selector(practice.ReviewPolicy())
        plan = selector.plans([anchor], [anchor], opts, {}, time.time())[0]
        policy = {"direction": "en_nl", "model": self.provider.model, "reasoning": self.provider.reasoning,
                  "prompt_version": sentence_generate.PROMPT_VERSION}
        generated = self.provider.generate([self.engine._document(plan)], opts.direction, [])[0]
        expected = []
        for idx in range(5):
            body = {**generated["draft"], "dutch": f"Ik lees over {anchor['surface']} in boek {idx}.",
                    "english": f"I read about {anchor['meaning']} in book {idx}."}
            draft = sentences.Draft.model_validate(body)
            verdict = sentences.Verdict.model_validate(generated["verdict"])
            question = self.db.sentence_cache(draft, verdict, plan, policy)
            session = self.db.sentence_create(opts, [question], practice.ReviewPolicy().document())
            if idx < 4:
                opened = self.db.sentence_current(session)
                self.db.sentence_submit(opened["id"], "PRIVATE_LEARNER_ANSWER", 1000)
                expected.insert(0, {"dutch": body["dutch"], "english": body["english"]})
        examples = self.db.sentence_examples([anchor["word_id"]], "en_nl")[anchor["word_id"]]
        self.assertEqual(examples, expected[:3])
        plan["recent_sentences"] = examples
        document = self.engine._document(plan)
        self.assertNotIn("PRIVATE_LEARNER_ANSWER", json.dumps(document))
        self.assertEqual(document["recent_sentences"], expected[:3])

    def test_recent_examples_include_words_previously_used_as_companions(self):
        """Use the word's actual sentence exposure, not just its anchor role; e.g. a weak companion cannot receive the same context under a different question ID."""

        opts = self.opts.model_copy(update={"count": 1, "words": 2})
        session = self.engine.start(opts, lambda _: None)
        row = self.db.sentence_current(session)
        companion = next(word["word_id"] for word in row["content"]["words"] if word["word_id"] != row["anchor_word_id"])
        examples = self.db.sentence_examples([companion], "en_nl")
        self.assertEqual(examples[companion], [{key: row["content"][key] for key in ("dutch", "english")}])

    def test_translation_and_dictation_offer_choices_and_save_only_used_targets(self):
        """Allow the last companion or the anchor alone, e.g. dictation can skip all five companions."""

        engine = sentence_service.SentenceService(self.db, self.provider, speech=listening_fixtures.Speech())
        generate = self.provider.generate

        def choose(plans, direction, focus):
            """Use a suitable companion from anywhere in the pool or the anchor alone."""

            plan, = plans
            anchor, *companions = plan["words"]
            self.assertEqual(anchor["word_id"], plan["anchor_word_id"])
            self.assertEqual((len(companions), plan["max_words"]), (5, 2))
            self.assertTrue(all("context" not in word for word in companions))
            selected = [anchor] if direction == sentences.Direction.LISTEN else [anchor, companions[-1]]
            return generate([dict(plan, words=selected)], direction, focus)

        for direction in sentences.Direction:
            with self.subTest(direction=direction):
                opts = self.opts.model_copy(update={"direction": direction, "count": 1, "words": 2})
                with mock.patch.object(self.provider, "generate", side_effect=choose):
                    session = engine.start(opts, lambda _: None)
                row = self.db.sentence_current(session)
                selected = generate.call_args.args[0][0]["words"]
                expected = [word["word_id"] for word in selected]
                self.assertEqual([word["word_id"] for word in row["content"]["words"]], expected)
                with self.db._connect() as conn:
                    links = conn.execute("SELECT l.word_id FROM sentence_question_words w JOIN vocab_lookups l ON l.id=w.lookup_id "
                                         "WHERE w.question_id=? ORDER BY w.ordinal", (row["question_id"],))
                    self.assertEqual([link[0] for link in links], expected)
