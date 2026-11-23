"""Verify audited passage stock, partial grades and vocabulary communication with real SQLite.

Example: a typed lexical confusion becomes ordinary vocabulary without counting as another submitted answer.
"""

import json
import os
import concurrent.futures
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import psutil

from tests import comprehension_fixtures as fixtures
from tests import listening_fixtures as speech
from tests import sentence_fixtures
from yt_whisper_subs import comprehension as comp
from yt_whisper_subs import comprehension_audio
from yt_whisper_subs import comprehension_service
from yt_whisper_subs import comprehension_stats
from yt_whisper_subs import correction_queue
from yt_whisper_subs import library_db
from yt_whisper_subs import practice_activity
from yt_whisper_subs import vocabulary_learning as learning


class ComprehensionTests(unittest.TestCase):
    """Test real persistence and scheduling boundaries offline; e.g. a provider failure never becomes a score of zero."""

    def setUp(self):
        """Seed only synthetic source vocabulary; e.g. no user catalog, private answers or network credentials are used."""

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = library_db.LibraryDb(Path(folder.name) / 'catalog.sqlite3')
        self.db.initialize()
        sentence_fixtures.seed(self.db)
        self.provider, self.speech = fixtures.Provider(), speech.Speech()
        self.engine = comprehension_service.ComprehensionService(self.db, self.provider, speech=self.speech)
        self.opts = fixtures.options()

    def _start(self, opts=None):
        """Create a complete fixture round; e.g. exact counts and audit rules still pass through production code."""

        return self.engine.start(opts or self.opts, lambda _: None)

    def _open(self, session, *, kind='mc'):
        """Expose one requested fixture question; e.g. cache preparation itself contributes no shown questions."""

        row = next(row for row in self.db.comprehension_history(session) if row['kind'] == kind)
        return self.db.comprehension_open(row['id'])

    def test_standby_is_consumed_once_without_api_calls_or_practice_exposure(self):
        """Reuse checked text after restart; e.g. opening a ready round still works with both providers unavailable."""

        ready = self.engine.start(self.opts, lambda _: None, background=True)
        self.assertEqual(practice_activity.read(self.db).answers, 0)
        self.assertTrue(all(row['shown_at'] is None for row in self.db.comprehension_history(ready)))
        self.provider.available = self.provider.audit_available = False
        reopened = comprehension_service.ComprehensionService(self.db, self.provider, speech=self.speech)
        self.assertEqual(reopened.start(self.opts, lambda _: None), ready)
        self.assertEqual(len(self.provider.generated), 1)
        self.assertEqual(len(self.provider.audited), 1)
        self.assertFalse(reopened.needs(self.opts))

    def test_reduced_question_mix_reuses_unopened_passage_and_audio(self):
        """Both modes reuse exactly the requested subset and keep the original approved text and questions."""

        for direction in comp.Direction:
            with self.subTest(direction=direction):
                opts = self.opts.model_copy(update={'direction': direction, 'mc': 3, 'open': 2})
                original = self.engine.start(opts, lambda _: None, background=True)
                source = self.db.comprehension_session(original)['passage_id']
                checked = len(self.provider.audited)
                rendered = len(self.speech.calls)
                resized = opts.model_copy(update={'mc': 1, 'open': 2})
                with mock.patch.object(self.provider, "generate", side_effect=AssertionError("Unexpected regeneration")):
                    ready = self.engine.start(resized, lambda _: None, background=True)
                    session = self.engine.start(resized, lambda _: None, cached_first=False)
                self.assertEqual(session, ready)
                self.assertEqual(self.db.comprehension_session(session)['passage_id'], source)
                self.assertEqual(self.db.comprehension_session(original)['status'], 'stopped')
                rows = self.db.comprehension_history(session)
                self.assertEqual([row['kind'] for row in rows], ['mc', 'open', 'open'])
                self.assertEqual([row['ordinal'] for row in rows], [1, 2, 3])
                self.assertEqual(len(self.db.comprehension_history(original)), 5)
                self.assertEqual(len(self.provider.audited), checked)
                self.assertEqual(len(self.speech.calls), rendered)

    def test_exact_question_counts_and_bounded_quality_repair(self):
        """Repair a leaked answer without regenerating the source or changing either question count."""

        self.provider.reject_first = True
        opts = fixtures.options(mc=4, open=2)
        session = self._start(opts)
        rows = self.db.comprehension_history(session)
        self.assertEqual([sum(r['kind'] == kind for r in rows) for kind in ('mc', 'open')], [4, 2])
        self.assertEqual(len(self.provider.generated), 1)
        self.assertEqual([[item.key for item in batch] for batch in self.provider.repaired], [[1]])
        before, after = self.provider.audited
        self.assertEqual((before.paragraphs, before.targets, before.questions[1:]),
                         (after.paragraphs, after.targets, after.questions[1:]))
        self.assertNotEqual(before.questions[0].prompt, after.questions[0].prompt)
        self.assertIn('verraadt', self.db.comprehension_passage(1)['error'])

    def test_incomplete_audit_retains_generated_text_for_retry(self):
        """A missing reviewer result cannot trigger a paid rewrite or silently approve a question."""

        audit = self.provider.audit
        with mock.patch.object(self.provider, 'audit', side_effect=lambda passage, level: audit(passage, level).model_copy(update={'questions': []})):
            with self.assertRaisesRegex(comp.AuditIncomplete, 'niet volledig'):
                self._start()
        self.assertEqual(len(self.provider.audited), 2)
        self.assertEqual(self.db.comprehension_passage(1)['error'], '')
        self.assertIsNone(self.db.comprehension_active('read'))
        self._start()
        self.assertEqual(len(self.provider.generated), 1)

    def test_persistent_question_rejection_never_publishes_a_round(self):
        """Bound whole drafts and focused repairs even if every audit flags a question."""

        audit = self.provider.audit

        def reject(passage, level):
            """Flag an answer leak on every independently checked draft."""

            verdict = audit(passage, level)
            verdict.questions[0].valid = False
            return verdict

        with mock.patch.object(self.provider, 'audit', side_effect=reject), self.assertRaises(comp.DraftRejected):
            self._start()
        self.assertEqual(len(self.provider.generated), 3)
        self.assertEqual(len(self.provider.repaired), 3)
        self.assertEqual(len(self.provider.audited), 6)
        self.assertEqual(self.provider.generated[-1]['words'], [])
        self.assertIsNone(self.db.comprehension_active('read'))

    def test_a1_audit_replaces_off_level_passage_and_accepts_short_paragraphs(self):
        """Keep shorter paragraphs after the audit rejects a passage for exceeding A1 difficulty."""

        opts = fixtures.options(level='A1', direction=comp.Direction.LISTEN)
        paragraphs = [
            'De buurman ziet de fietsen. De kinderen lopen naar school. Het besluit is nog niet definitief.',
            'Noor gaat naar de winkel. Er staan veel fietsen voor de deur.',
            'Noor loopt naar huis. Haar buurman zit buiten. Hij drinkt koffie met Noor.',
        ]
        generate, audit = self.provider.generate, self.provider.audit

        def generate_short_retry(document):
            """Use short paragraphs after the first audit explains the level problem."""

            passage = generate(document)
            return passage.model_copy(update={'paragraphs': paragraphs}) if document['previous_rejection'] else passage

        def check_level(passage, level):
            """Reject the original fixture wording when A1 was requested."""

            self.assertEqual(level, comp.Level.A1)
            verdict = audit(passage, level)
            verdict.passage_issue = '' if passage.paragraphs == paragraphs else 'De zinnen zijn te moeilijk voor A1.'
            return verdict

        with (
            mock.patch.object(self.provider, 'generate', side_effect=generate_short_retry),
            mock.patch.object(self.provider, 'audit', side_effect=check_level),
        ):
            session = self._start(opts)

        self.assertEqual(self.provider.generated[1]['previous_rejection'], 'De zinnen zijn te moeilijk voor A1.')
        self.assertEqual(len(self.provider.generated[0]['words']), opts.words)
        self.assertEqual(self.provider.generated[1]['words'], [])
        self.assertEqual(self.provider.generated[0]['options'], self.provider.generated[1]['options'])
        pid = self.db.comprehension_session(session)['passage_id']
        self.assertEqual(self.db.comprehension_passage(pid)['content']['paragraphs'], paragraphs)
        self.assertTrue(self.engine.audio.ready(pid))

    def test_omitted_questions_never_publish_and_are_bounded(self):
        """Keep paid rejected drafts for diagnosis without accepting missing content; e.g. a smaller output cannot silently become the requested round."""

        self.provider.omit_question = True
        with self.assertRaises(ValueError):
            self._start()
        self.assertEqual(len(self.provider.generated), 3)
        self.assertIsNone(self.db.comprehension_active('read'))
        with self.db._connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM comprehension_passages').fetchone()[0], 3)

    def test_only_used_words_are_saved_and_unsupported_tags_do_not_reject_questions(self):
        """Keep valid content when the writer skips candidates or mislabels a question as lexical."""

        generate, audit = self.provider.generate, self.provider.audit

        def use_one(document):
            """Pick a single suitable candidate from the ranked list."""

            return generate(dict(document, words=document['words'][:1]))

        def review(passage, level):
            """Remove one unsupported tag while clearing the rest of the exercise."""

            verdict = audit(passage, level)
            verdict.questions[0].unsupported_word_ids = passage.questions[0].word_ids
            return verdict

        with (
            mock.patch.object(self.provider, 'generate', side_effect=use_one),
            mock.patch.object(self.provider, 'audit', side_effect=review),
        ):
            session = self._start()

        pid = self.db.comprehension_session(session)['passage_id']
        first, second, *_ = self.db.comprehension_history(session)
        self.assertEqual(first['content']['word_ids'], [])
        with self.db._connect() as conn:
            used = [row[0] for row in conn.execute('SELECT word_id FROM comprehension_targets WHERE passage_id=?', (pid,))]
        self.assertEqual(used, second['content']['word_ids'])
        self.assertEqual(len(used), 1)
        self.assertEqual(len(self.provider.audited), 1)
        self.assertFalse(self.provider.repaired)

    def test_bad_evidence_is_repaired_before_independent_review(self):
        """Replace a bad quote on a fixed passage before asking the auditor to solve it."""

        generate, repair = self.provider.generate, self.provider.repair_questions
        quote = 'Het besluit is nog niet definitief.'

        def bad_quote(document):
            """Simulate a quote that paraphrases the source."""

            passage = generate(document)
            passage.questions[0].evidence[0].quote = 'Het besluit is voorlopig.'
            return passage

        def fix_quote(passage, issues, level):
            """Repair the verbatim quote with the original passage still frozen."""

            fixed = repair(passage, issues, level).model_copy(deep=True)
            fixed.questions[0].evidence[0].quote = quote
            return fixed

        with (
            mock.patch.object(self.provider, 'generate', side_effect=bad_quote),
            mock.patch.object(self.provider, 'repair_questions', side_effect=fix_quote),
        ):
            session = self._start()

        self.assertEqual(len(self.provider.generated), 1)
        self.assertEqual(len(self.provider.audited), 1)
        self.assertIn('bewijsplaats', self.provider.repaired[0][0].explanation)
        self.assertEqual(self._open(session)['content']['evidence'][0]['quote'], quote)

    def test_passage_works_without_eligible_vocabulary(self):
        """Allow a comprehension-only exercise, e.g. a new learner has no saved Dutch words."""

        with mock.patch.object(self.db, 'word_candidates', return_value=[]):
            session = self._start()
        self.assertEqual(self.provider.generated[0]['words'], [])
        self.assertEqual(len(self.db.comprehension_history(session)), self.opts.count)

    def test_passage_writer_gets_more_candidates_than_it_can_use(self):
        """A five-word limit offers 25 choices while local validation still rejects six targets."""

        opts = fixtures.options(words=5)
        template = self.db.word_candidates('nl_en')[0]
        rows = [dict(template, word_id=i, lookup_id=i, lemma=f'woord{i}') for i in range(100, 130)]
        with mock.patch.object(self.db, 'word_candidates', return_value=rows):
            session = self._start(opts)
        request = self.provider.generated[0]
        ids = [word['word_id'] for word in request['words']]
        self.assertEqual(len(set(ids)), 25)
        self.assertEqual(request['options']['words'], 5)
        pid = self.db.comprehension_session(session)['passage_id']
        passage = comp.Passage.model_validate(self.db.comprehension_passage(pid)['content'])
        passage.targets = [comp.Target(word_id=wid, forms=['gemeente']) for wid in ids[:6]]
        with self.assertRaisesRegex(comp.DraftRejected, 'meer dan 5'):
            passage.check(opts, set(ids))

    def test_audit_outage_reuses_its_saved_draft(self):
        """Checkpoint between generation and review; e.g. retrying an unavailable auditor does not generate another article."""

        self.provider.audit_available = False
        with self.assertRaises(OSError):
            self._start()
        self.provider.audit_available, self.provider.available = True, False
        self._start()
        self.assertEqual(len(self.provider.generated), 1)

    def test_audio_failure_reuses_approved_text_and_repairs_only_missing_speed(self):
        """Materialize paragraph speech independently from paid text; e.g. a broken slow WAV never purchases another passage."""

        opts = fixtures.options(direction=comp.Direction.LISTEN)
        self.speech.available = False
        with self.assertRaises(OSError):
            self._start(opts)
        self.speech.available, self.provider.available = True, False
        session = self._start(opts)
        state = self.db.comprehension_session(session)
        rows = self.engine.audio.rows(state['passage_id'])
        self.assertGreater(len(rows), 3)
        rows[0]['paths']['slow'].unlink()
        self.assertFalse(self.engine.audio.ready(state['passage_id']))
        before = len(self.speech.calls)
        self.engine.prepare_media(state['passage_id'], opts, lambda _: None)
        self.assertEqual(len(self.speech.calls), before + 1)
        self.assertEqual(set(self.speech.calls[-1][1]), {'slow'})
        self.assertEqual(len(self.provider.generated), 1)

    def test_reading_and_listening_do_not_repeat_a_previously_exposed_passage(self):
        """Preserve cross-modality novelty while sharing word priorities; e.g. listening cannot be solved by remembering the article just read."""

        read = self._start()
        self._open(read)
        listen = self._start(fixtures.options(direction=comp.Direction.LISTEN))
        self.assertNotEqual(self.db.comprehension_session(read)['passage_id'], self.db.comprehension_session(listen)['passage_id'])

    def test_missing_current_narration_gets_prepared_even_with_a_warm_spare(self):
        """Repair the passage that will actually open; e.g. ready spare audio cannot mask a missing clip in an ongoing round."""

        opts = fixtures.options(direction=comp.Direction.LISTEN)
        active = self._start(opts)
        row = self._open(active)
        self.db.comprehension_submit(row['id'], row['content']['correct_idx'], 1000)
        distinct = [p.replace('zou eventueel', 'zou misschien') for p in fixtures.PARAGRAPHS]
        with mock.patch.object(fixtures, 'PARAGRAPHS', distinct):
            standby = self.engine.start(opts, lambda _: None, background=True)
        self.assertNotEqual(active, standby)
        self.assertFalse(self.engine.needs(opts))
        passage = self.db.comprehension_session(active)['passage_id']
        self.engine.audio.rows(passage)[0]['paths']['slow'].unlink()
        next_passage = self.db.comprehension_session(standby)['passage_id']
        self.assertTrue(self.engine.audio.ready(next_passage))
        self.assertTrue(self.engine.needs(opts))
        before = len(self.speech.calls)
        self.provider.available = False

        self.assertEqual(self.engine.start(opts, lambda _: None, background=True), active)
        self.assertTrue(self.engine.audio.ready(passage))
        self.assertEqual(len(self.speech.calls), before + 1)
        self.assertFalse(self.engine.needs(opts))
        self.assertEqual(self.db.comprehension_active('listen'), active)
        self.assertEqual(len(self.provider.generated), 2)

    def test_choices_are_local_immutable_and_do_not_rate_tagged_words(self):
        """A passage choice measures comprehension, e.g. a wrong inference leaves the tagged word unassessed."""

        row = self._open(self._start())
        wrong = (row['content']['correct_idx'] + 1) % 4
        first = self.db.comprehension_submit(row['id'], wrong, 1500)
        again = self.db.comprehension_submit(row['id'], row['content']['correct_idx'], 9000)
        self.assertEqual(first['answer_text'], again['answer_text'])
        self.assertEqual(again['outcome'], 'wrong')
        word = next(w for w in self.db.word_candidates('nl_en') if w['word_id'] == row['content']['word_ids'][0])
        self.assertEqual((word['attempts'], word['failures'], word['card_json']), (0, 0, None))
        self.assertEqual(practice_activity.read(self.db).answers, 1)
        self.assertEqual(self.provider.graded, [])
        with self.db._connect() as conn:
            conn.execute("DELETE FROM settings WHERE key='practice_skill_reviews_rebuilt'")
            self.db._comprehension_observe(conn, row, word["lookup_id"], False, "target", "nl_en")
            self.db._comprehension_replay(conn, row["id"])
        self.assertTrue(any(w["card_json"] for w in self.db.word_candidates("nl_en")))
        self.db.initialize()
        self.assertTrue(all(w["card_json"] is None for w in self.db.word_candidates("nl_en")))
        self.assertEqual(self.db.comprehension_get(row["id"])["outcome"], "wrong")

    def test_open_partial_credit_and_queue_failure_do_not_invent_zeroes(self):
        """Persist before correction and compute content points locally; e.g. a pending answer has no accuracy score."""

        row = self._open(self._start(), kind='open')
        self.db.comprehension_submit(row['id'], 'De gemeente wil minder verkeer.', 2000)
        pending = comprehension_stats.snapshot(self.db, 'read')
        self.assertIsNone(pending['score'])
        self.assertEqual(pending['pending'], 1)
        queue = correction_queue.Queue(self.db, 'comprehension')
        owner = f'{os.getpid()}:{psutil.Process().create_time()}:fixture'
        self.assertEqual(queue.claim(owner)['id'], row['id'])
        queue.done(row['id'], owner, error='network')
        self.assertIsNone(queue.claim(owner))
        queue.retry(row['id'])
        self.assertEqual(queue.claim(owner)['id'], row['id'])
        graded = self.engine.check(row['id'], lambda _: None)
        queue.done(row['id'], owner)
        self.assertEqual(graded['score'], .5)
        self.assertTrue(graded['language_correct'])
        self.assertEqual(practice_activity.read(self.db).answers, 1)
        self.assertEqual(practice_activity.read(self.db).week[-1].comprehension_read, 1)

    def test_typed_real_word_confusion_adds_both_lemmas_and_shared_failures(self):
        """Connect open comprehension to ordinary word practice; e.g. zekerder for veiliger teaches both safely sourced dictionary identities."""

        row = self._open(self._start(), kind='open')
        answer = 'De straten worden zekerder.'
        self.db.comprehension_submit(row['id'], answer, 2300)
        grade = self.provider.grade(dict(question=row['content'], learner_answer=answer))
        fix = learning.Correction(typed='zekerder', dutch=['veiliger'], english=['safer'],
                                 intended=learning.Lexeme(lemma='veilig', meaning='safe', part_of_speech='adjective'),
                                 confused=learning.Lexeme(lemma='zeker', meaning='certain', part_of_speech='adjective'),
                                 note='Veilig gaat over veiligheid; zeker gaat over zekerheid.')
        self.provider.grade_result = grade.model_copy(update=dict(corrected='De straten worden veiliger.', corrected_english='The streets become safer.',
                                                                  language_correct=False, vocabulary=[fix]))
        self.provider.verify_available = False
        with self.assertRaises(OSError):
            self.engine.check(row['id'], lambda _: None)
        calls = len(self.provider.graded)
        self.assertIsNotNone(self.db.comprehension_proposal(row['id']))
        self.assertIsNone(self.db.comprehension_get(row['id'])['score'])
        original_model = self.provider.model
        self.provider.model = 'changed-after-proposal'
        self.provider.available, self.provider.verify_available = False, True
        self.engine.check(row['id'], lambda _: None)
        self.assertEqual(len(self.provider.graded), calls)
        self.assertEqual(self.db.comprehension_get(row['id'])['model'], original_model)
        words = {w['lemma']: w for w in self.db.word_candidates('en_nl')}
        self.assertEqual([words[w]['failures'] for w in ('veilig', 'zeker')], [1, 1])
        self.assertEqual([words[w]['clicks'] for w in ('veilig', 'zeker')], [0, 0])
        self.assertEqual(practice_activity.read(self.db).answers, 1)
        self.db.comprehension_flag(row['passage_id'], 'Onduidelijke bron')
        self.assertFalse({'veilig', 'zeker'} & {w['lemma'] for w in self.db.word_candidates('en_nl')})
        self.assertEqual(practice_activity.read(self.db).answers, 1)
        self.assertIsNone(comprehension_stats.snapshot(self.db, 'read')['score'])

    def test_only_one_spare_is_built_after_answering_and_hidden_until_consumed(self):
        """Bound paid stock without affecting current work; e.g. repeated scheduler ticks reuse the same unopened passage."""

        session = self._start()
        row = self._open(session)
        self.assertFalse(self.engine.needs(self.opts))
        self.db.comprehension_submit(row['id'], row['content']['correct_idx'], 1000)
        self.assertTrue(self.engine.needs(self.opts))
        spare = self.engine.start(self.opts, lambda _: None, background=True)
        self.assertEqual(self.db.comprehension_active('read'), session)
        self.assertFalse(self.engine.needs(self.opts))
        self.assertEqual(self.engine.start(self.opts, lambda _: None, background=True), spare)
        self.assertTrue(all(r['shown_at'] is None for r in self.db.comprehension_history(spare)))
        self.assertEqual(len(self.provider.generated), 2)
        self.assertEqual(self.engine.start(self.opts, lambda _: None, cached_first=False), spare)

    def test_changed_counts_prepare_stock_beside_an_untouched_active_round(self):
        """Changing counts should warm a matching round while the previous exercise stays resumable."""

        active = self._start()
        opts = self.opts.model_copy(update={'mc': 1})
        self.assertTrue(self.engine.needs(opts))
        ready = self.engine.start(opts, lambda _: None, background=True)
        self.assertNotEqual(ready, active)
        self.assertEqual(self.db.comprehension_active('read'), active)
        self.assertFalse(self.engine.needs(opts))
        self.assertEqual(len(self.db.comprehension_history(ready)), opts.count)

    def test_reporting_while_a_grade_is_pending_cannot_publish_late_feedback(self):
        """Make reporting win over late results; e.g. a bad passage adds activity but no review successes or failures."""

        row = self._open(self._start(), kind='open')
        self.db.comprehension_submit(row['id'], 'De gemeente wil minder verkeer.', 2000)
        self.db.comprehension_flag(row['passage_id'], 'Verkeerde bron')
        graded = self.engine.check(row['id'], lambda _: None)
        self.assertEqual(graded['outcome'], 'invalid')
        self.assertIsNone(graded['score'])
        self.assertFalse(self.provider.graded)
        self.assertEqual(practice_activity.read(self.db).answers, 1)

    def test_corrupt_source_evidence_never_passes_local_approval(self):
        """Require evidence to exist in the cited paragraph; e.g. plausible invented quotes cannot justify a nuanced answer."""

        session = self._start()
        source = self.db.comprehension_passage(self.db.comprehension_session(session)['passage_id'])
        passage = comp.Passage.model_validate(source['content'])
        passage.questions[0].evidence[0].quote = 'Deze zin staat nergens in de tekst.'
        with self.assertRaisesRegex(ValueError, 'bewijsplaats'):
            passage.check(self.opts, {t.word_id for t in passage.targets})

    def test_concurrent_modalities_reserve_different_passages_from_shared_drafts(self):
        """Serialize only text reservation, not local narration; e.g. both startup workers cannot consume the same interrupted audit."""

        self.provider.audit_available = False
        with self.assertRaises(OSError):
            self._start()
        self.provider.audit_available = True
        audit = self.provider.audit

        def delayed(passage, level):
            """Leave an observable race window at the independent audit; e.g. another modality may inspect the shared pool concurrently."""

            time.sleep(.03)
            return audit(passage, level)

        self.provider.audit = delayed
        opts = [self.opts, self.opts.model_copy(update=dict(direction=comp.Direction.LISTEN))]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(self.engine.start, opt, lambda _: None, background=True) for opt in opts]
            sessions = [job.result(timeout=10) for job in jobs]
        passages = {self.db.comprehension_session(session)['passage_id'] for session in sessions}
        self.assertEqual(len(passages), 2)
        self.assertEqual(len(self.provider.generated), 2)

    def test_segmenting_preserves_every_word_and_bounds_native_generation(self):
        """Avoid lost article content and oversized Zonos jobs; e.g. a long sentence is split only at whitespace."""

        paragraphs = [' '.join(['langwoord'] * 100), 'Dit is een korte zin. En hier staat nog een zin!']
        chunks = comprehension_audio.segments(paragraphs)
        self.assertEqual(' '.join(c['text'] for c in chunks), ' '.join(paragraphs))
        self.assertTrue(all(len(c['text']) <= 150 for c in chunks))


if __name__ == '__main__':
    unittest.main()
