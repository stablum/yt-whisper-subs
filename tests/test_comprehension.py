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

    def test_exact_question_counts_and_bounded_quality_repair(self):
        """Reject an undersized round instead of exposing it; e.g. a repaired set still contains every requested question."""

        self.provider.reject_first = True
        opts = fixtures.options(mc=4, open=2)
        session = self._start(opts)
        rows = self.db.comprehension_history(session)
        self.assertEqual([sum(r['kind'] == kind for r in rows) for kind in ('mc', 'open')], [4, 2])
        self.assertEqual(len(self.provider.generated), 2)
        self.assertTrue(self.provider.generated[1]['previous_rejection'])
        self.assertEqual(self.provider.generated[0]['words'], self.provider.generated[1]['words'])

    def test_omitted_questions_never_publish_and_are_bounded_to_two_drafts(self):
        """Keep paid rejected drafts for diagnosis without accepting missing content; e.g. a smaller output cannot silently become the requested round."""

        self.provider.omit_question = True
        with self.assertRaises(ValueError):
            self._start()
        self.assertEqual(len(self.provider.generated), 2)
        self.assertIsNone(self.db.comprehension_active('read'))
        with self.db._connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM comprehension_passages').fetchone()[0], 2)

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

    def test_choices_are_local_immutable_and_feed_the_shared_word_schedule(self):
        """Use one actual answer event with the same failed-word treatment; e.g. retries do not create duplicate quiz activity."""

        row = self._open(self._start())
        wrong = (row['content']['correct_idx'] + 1) % 4
        first = self.db.comprehension_submit(row['id'], wrong, 1500)
        again = self.db.comprehension_submit(row['id'], row['content']['correct_idx'], 9000)
        self.assertEqual(first['answer_text'], again['answer_text'])
        self.assertEqual(again['outcome'], 'wrong')
        word = next(w for w in self.db.word_candidates('nl_en') if w['word_id'] == row['content']['word_ids'][0])
        self.assertEqual((word['attempts'], word['failures']), (1, 1))
        self.assertEqual(practice_activity.read(self.db).answers, 1)
        self.assertEqual(self.provider.graded, [])

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

        def delayed(passage):
            """Leave an observable race window at the independent audit; e.g. another modality may inspect the shared pool concurrently."""

            time.sleep(.03)
            return audit(passage)

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
