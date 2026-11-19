"""Coordinate audited passage stock, adaptive words and durable corrections.

Example: changing a narrator reuses the same approved article while an unfinished audit survives restart.
"""

import json
import time

from yt_whisper_subs import i18n
from yt_whisper_subs import comprehension as comp
from yt_whisper_subs import comprehension_audio
from yt_whisper_subs import comprehension_generate
from yt_whisper_subs import practice
from yt_whisper_subs import practice_ready
from yt_whisper_subs import practice_round
from yt_whisper_subs import word_select
from yt_whisper_subs import verb_practice


class ComprehensionService:
    """Share one linguistic strategy across reading and listening; e.g. only the presentation modality changes."""

    def __init__(self, db, provider=None, *, speech=None) -> None:
        """Inject deterministic linguistic/audio providers in tests; e.g. native integration runs offline."""

        self._db = db
        self._provider = provider or comprehension_generate.Generator()
        self.audio = comprehension_audio.Audio(db, speech)

    @property
    def policy(self) -> dict:
        """Identify reusable generation independently from UI state; e.g. an upgraded prompt cannot silently reuse an older audit."""

        return dict(model=self._provider.model, reasoning=self._provider.reasoning, version=comprehension_generate.VERSION)

    def needs(self, opts: comp.Options) -> bool:
        """Maintain one unopened spare after practice begins; e.g. unused paid passages are consumed before new ones are generated."""

        if self._missing_resume_audio(opts):
            return True
        if ready := self._db.comprehension_ready(opts, self.policy):
            return opts.direction == comp.Direction.LISTEN and not self.audio.ready(ready['passage_id'])
        active = self._db.comprehension_active(opts.direction.value)
        return not active or bool(self._db.comprehension_summary(active)['submitted'])

    def _missing_resume_audio(self, opts: comp.Options) -> dict | None:
        """Check the shared voice on resumed work, e.g. a warm spare cannot hide a missing active recording."""

        if opts.direction != comp.Direction.LISTEN:
            return None
        if session_id := self._db.comprehension_resume(opts.direction.value):
            session = self._db.comprehension_session(session_id)
            if not self.audio.ready(session['passage_id']):
                return session
        return None

    def start(self, opts: comp.Options, report, *, cached_first=True, background=False) -> str:
        """Prepare exactly the requested two question counts under shared ownership; e.g. a failed narrator never purchases a replacement passage."""

        track = 'comprehension:' + opts.direction.value
        with practice_ready.preparing(self._db, track, report) as progress:
            if background and (session := self._missing_resume_audio(opts)):
                self.prepare_media(session['passage_id'], opts, progress)
                return session['id']
            active = self._db.comprehension_active(opts.direction.value)
            if active and cached_first and not background:
                session = self._db.comprehension_session(active)
                self.prepare_media(session['passage_id'], opts, progress)
                return active
            if ready := self._db.comprehension_ready(opts, self.policy):
                self.prepare_media(ready['passage_id'], opts, progress)
                return ready['id'] if background else self._db.comprehension_use_ready(ready['id'], opts)
            if active and background and not self._db.comprehension_summary(active)['submitted']:
                return active
            session = self._reserve(opts, progress)
            passage_id = self._db.comprehension_session(session)['passage_id']
            self.prepare_media(passage_id, opts, progress)
            return session if background else self._db.comprehension_use_ready(session, opts)

    def _reserve(self, opts, report) -> str:
        """Reserve audited text before releasing shared pool ownership; e.g. concurrent reading/listening fills cannot claim the same unfinished draft."""

        # Audio runs after this shared text lease so slow speech does not block reading.
        with practice_ready.preparing(self._db, 'comprehension:content', report) as progress:
            row = self._db.comprehension_pool(opts, self.policy)
            if row is None:
                row = self._generate(opts, progress)
            for attempt in range(2):
                passage_id = row['id']
                if row['validation_json'] is not None:
                    break
                passage = comp.Passage.model_validate_json(row['body_json'])
                try:
                    passage.check(opts, {t['word_id'] for t in json.loads(row['targets_json'])})
                    progress('Tekst en vragen onafhankelijk controleren…')
                    verdict = self._provider.audit(passage)
                    verdict.check(passage)
                except ValueError as exc:
                    self._db.comprehension_reject(passage_id, str(exc))
                    if attempt:
                        raise
                    row = self._generate(opts, progress, rejected=row, reason=str(exc))
                    continue
                self._db.comprehension_approve(passage_id, verdict)
                break
            return self._db.comprehension_create(passage_id, opts, practice.ReviewPolicy().document(), background=True)

    def _generate(self, opts, report, *, rejected=None, reason='') -> dict:
        """Use the existing FSRS/error-weighted word selector without multiplying vocabulary constraints; e.g. mastered future words defer naturally."""

        if rejected:
            targets = json.loads(rejected['targets_json'])
        else:
            rows = self._db.word_candidates('nl_en')
            if not rows:
                raise practice_round.NothingToPractice('no_words', i18n.tr('Save some Dutch words with translations first.'))
            selection = practice.RoundOptions(direction=practice.Direction.NL_EN, count=opts.words, include_early=opts.include_early)
            chosen = word_select.Selector(practice.ReviewPolicy()).select(rows, selection, time.time())
            if not chosen:
                raise practice_round.NothingToPractice('not_due', i18n.tr('These words are not due yet. Enable early review to continue.'))
            targets = [{key: row[key] for key in ('word_id', 'lookup_id', 'lemma', 'surface', 'lemma_meaning', 'meaning', 'part_of_speech')} for row in chosen]
        with self._db._connect() as conn:
            skills = [dict(row) for row in conn.execute("SELECT json_extract(q.body_json,'$.skill') AS skill,COUNT(*) AS attempts,"
                        "SUM(p.outcome='wrong') AS failures FROM comprehension_presentations p JOIN comprehension_questions q ON q.id=p.question_id "
                        "JOIN comprehension_sessions s ON s.id=p.session_id WHERE s.direction=? AND p.outcome IN ('correct','wrong') GROUP BY skill", (opts.direction.value,))]
        document = dict(options=json.loads(opts.content_key()), words=targets, weak_skills=skills,
                        recent_titles=self._db.comprehension_recent(), previous_rejection=reason,
                        previous_draft=json.loads(rejected['body_json']) if rejected else None)
        report(i18n.tr('Preparing a Dutch text with exactly the requested questions…'))
        passage = self._provider.generate(document)
        pid = self._db.comprehension_cache(passage, opts, targets, self.policy)
        return self._db.comprehension_passage(pid)

    def prepare_media(self, passage_id: int, opts, report) -> None:
        """Materialize speech only for listening; e.g. reading stock never loads a voice or waits for audio."""

        if opts.direction == comp.Direction.LISTEN:
            report(i18n.tr('Preparing and checking speech clips…'))
            self.audio.prepare(passage_id, report)

    def check(self, presentation_id: int, report) -> dict:
        """Grade one immutable open answer and retain partial credit; e.g. an outage cannot turn a pending answer into a zero."""

        row = self._db.comprehension_get(presentation_id)
        if row['answer_text'] is None:
            raise ValueError('Dien eerst je antwoord in.')
        if row['outcome'] is not None:
            return row
        question = comp.Question.model_validate(row['content'])
        if question.kind != 'open':
            raise ValueError('Meerkeuzevragen worden lokaal beoordeeld.')
        report(i18n.tr('Checking your answer and the meanings of words used…'))
        exact = verb_practice.normalized(row['answer_text']) == verb_practice.normalized(question.answer)
        grade = self._db.comprehension_proposal(presentation_id)
        policy = {key: row[key] for key in ('model', 'reasoning', 'version')} if grade is not None else None
        if grade is not None:
            exact = False
        elif exact:
            grade = comp.Grade(confident=True, criteria=[comp.CriterionResult(key=c.key, met=True, explanation=c.text) for c in question.rubric],
                               language_correct=True, corrected=row['answer_text'], corrected_english='', explanation='Het antwoord voldoet aan alle criteria.',
                               words=[comp.WordResult(word_id=wid, correct=True, explanation='Goedgekeurd antwoord.') for wid in question.word_ids], errors=[], vocabulary=[])
        else:
            passage = self._db.comprehension_passage(row['passage_id'])['content']
            grade = self._provider.grade(dict(paragraphs=passage['paragraphs'], question=question.model_dump(), learner_answer=row['answer_text']))
        grade.check(row['answer_text'], question)
        policy = policy or dict(model='approved_exact_match' if exact else self._provider.model,
                                reasoning='none' if exact else self._provider.reasoning, version=comprehension_generate.GRADE_VERSION)
        self._db.comprehension_proposed(presentation_id, grade, policy)
        if grade.vocabulary:
            report(i18n.tr('Independently checking new word meanings…'))
            accepted = self._provider.verify(grade.learning_document(row['answer_text']), grade.vocabulary)
            grade = grade.model_copy(update={'vocabulary': [fix for idx, fix in enumerate(grade.vocabulary) if idx in accepted]})
        return self._db.comprehension_grade(presentation_id, grade, policy)
