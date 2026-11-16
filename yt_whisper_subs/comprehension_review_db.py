"""Commit comprehension, writing and shared lexical evidence together.

Example: a partially correct explanation earns content points while a safely/zeker confusion enters ordinary word review.
"""

import json
import time

from yt_whisper_subs import comprehension as comp
from yt_whisper_subs import comprehension_schema as schema
from yt_whisper_subs import vocabulary_learning as learning


class ComprehensionReviewDbMixin:
    """Preserve first attempts and derive scheduling from real evidence; e.g. retries cannot double-count a difficult word."""

    def comprehension_proposal(self, presentation_id: int) -> comp.Grade | None:
        """Reuse paid semantic grading after a lexical verifier outage; e.g. retrying dictionary checks does not regrade the answer."""

        with self._connect() as conn:
            row = conn.execute('SELECT proposal_json FROM comprehension_checks WHERE presentation_id=?', (presentation_id,)).fetchone()
            return comp.Grade.model_validate_json(row[0]) if row and row[0] else None

    def comprehension_proposed(self, presentation_id: int, grade: comp.Grade, policy: dict) -> None:
        """Checkpoint a structurally grounded grade before independent vocabulary review; e.g. losing the network preserves completed reasoning."""

        with self._connect() as conn:
            conn.execute('UPDATE comprehension_checks SET proposal_json=? WHERE presentation_id=? AND EXISTS '
                         '(SELECT 1 FROM comprehension_presentations p WHERE p.id=presentation_id AND p.outcome IS NULL)',
                         (grade.model_dump_json(), presentation_id))
            conn.execute('UPDATE comprehension_presentations SET model=?,reasoning=?,version=? WHERE id=? AND outcome IS NULL',
                         (policy['model'], policy['reasoning'], policy['version'], presentation_id))

    def comprehension_submit(self, presentation_id: int, answer: str | int, elapsed_ms: int) -> dict:
        """Grade fixed choices locally and queue open answers atomically; e.g. the learner can continue while semantic correction runs."""

        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            raw = conn.execute(schema.PRESENTATION + ' WHERE p.id=?', (presentation_id,)).fetchone()
            if raw is None or raw['shown_at'] is None:
                raise ValueError('Open eerst de vraag.')
            if raw['answer_text'] is not None:
                return self._comprehension_row(raw)
            if raw['status'] != 'active' or raw['flagged_at'] is not None:
                raise ValueError('Deze ronde is niet meer actief.')
            question = comp.Question.model_validate_json(raw['body_json'])
            idx = answer if isinstance(answer, int) and not isinstance(answer, bool) else None
            if question.kind == 'mc':
                if idx not in range(4):
                    raise ValueError('Kies één antwoord.')
                text = question.choices[idx].text
            else:
                if not isinstance(answer, str) or not 1 <= len(answer.strip()) <= 4000:
                    raise ValueError('Schrijf een antwoord van 1–4000 tekens.')
                text = answer.strip()
            conn.execute('UPDATE comprehension_presentations SET answer_text=?,selected_idx=?,answered_at=?,elapsed_ms=? WHERE id=?',
                         (text, idx, time.time(), max(0, elapsed_ms), presentation_id))
            if question.kind == 'mc':
                row = dict(conn.execute(schema.PRESENTATION + ' WHERE p.id=?', (presentation_id,)).fetchone())
                correct = idx == question.correct_idx
                feedback = dict(correct=correct, corrected=question.choices[question.correct_idx].text,
                                explanation=question.choices[idx].explanation, evidence=[e.model_dump() for e in question.evidence])
                self._comprehension_result(conn, row, feedback, float(correct), None, {'model': 'audited_choice', 'reasoning': 'none', 'version': 1})
                for word_id in question.word_ids:
                    lookup = conn.execute('SELECT lookup_id FROM comprehension_targets WHERE passage_id=? AND word_id=?', (row['passage_id'], word_id)).fetchone()
                    self._comprehension_observe(conn, row, lookup[0], correct, 'target', 'nl_en')
                self._comprehension_replay(conn, presentation_id)
            else:
                conn.execute("INSERT INTO comprehension_checks(presentation_id,state) VALUES(?,'queued')", (presentation_id,))
            self._comprehension_complete(conn)
        return self.comprehension_get(presentation_id)

    @staticmethod
    def _comprehension_result(conn, row, feedback: dict, score: float, language_correct, policy: dict) -> None:
        """Save criterion-derived credit without guessing a percentage in SQL; e.g. two of three content points remains distinct from grammar accuracy."""

        conn.execute('UPDATE comprehension_presentations SET outcome=?,grade_json=?,score=?,language_correct=?,graded_at=?,model=?,reasoning=?,version=? WHERE id=?',
                     ('correct' if score == 1 else 'wrong', json.dumps(feedback, ensure_ascii=False), score, language_correct, time.time(),
                      policy['model'], policy['reasoning'], policy['version'], row['id']))

    def _comprehension_observe(self, conn, row, lookup_id: int, correct: bool, role: str, direction: str) -> None:
        """Record at most one observation per word and skill; e.g. a confirmed lexical mistake wins over an overlapping target success."""

        lookup = conn.execute('SELECT word_id,lemma FROM vocab_lookups WHERE id=?', (lookup_id,)).fetchone()
        if not self._word_allowed(conn, 'nl', lookup['lemma']):
            return
        word_id = lookup['word_id']
        conn.execute('INSERT INTO comprehension_assessments(presentation_id,word_id,lookup_id,direction,role,correct) VALUES(?,?,?,?,?,?) '
                     'ON CONFLICT(presentation_id,word_id,direction) DO UPDATE SET correct=MIN(correct,excluded.correct)',
                     (row['id'], word_id, lookup_id, direction, role, correct))

    def _comprehension_replay(self, conn, presentation_id: int) -> None:
        """Use the same chronological FSRS ledger as the word quiz; e.g. a reported passage can remove its influence exactly."""

        rows = conn.execute('SELECT word_id,direction FROM comprehension_assessments WHERE presentation_id=?', (presentation_id,)).fetchall()
        for row in rows:
            self._word_replay(conn, row['word_id'], row['direction'])

    @staticmethod
    def _comprehension_complete(conn) -> None:
        """Close only fully resolved sessions; e.g. an open correction still in flight remains resumable after the last submission."""

        conn.execute("UPDATE comprehension_sessions SET status='completed',finished_at=? WHERE status='active' AND NOT EXISTS "
                     "(SELECT 1 FROM comprehension_presentations p WHERE p.session_id=comprehension_sessions.id AND p.outcome IS NULL)", (time.time(),))

    def comprehension_grade(self, presentation_id: int, grade: comp.Grade, policy: dict) -> dict:
        """Commit independently evidenced lexical corrections with content grading; e.g. both the intended and real confused word become eligible vocabulary."""

        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            raw = conn.execute(schema.PRESENTATION + ' WHERE p.id=?', (presentation_id,)).fetchone()
            if raw is None or raw['answer_text'] is None:
                raise ValueError('Het antwoord is nog niet ingediend.')
            if raw['outcome'] is not None:
                return self._comprehension_row(raw)
            if raw['flagged_at'] is not None:
                raise ValueError('Deze tekst is gemeld.')
            row = dict(raw)
            question = comp.Question.model_validate_json(row['body_json'])
            grade.check(row['answer_text'], question)
            feedback = grade.model_dump(mode='json')
            feedback.update(correct=grade.score == 1, evidence=[e.model_dump() for e in question.evidence])
            self._comprehension_result(conn, row, feedback, grade.score, grade.language_correct, policy)
            for word in grade.words:
                if word.correct is not None:
                    lookup = conn.execute('SELECT lookup_id FROM comprehension_targets WHERE passage_id=? AND word_id=?', (row['passage_id'], word.word_id)).fetchone()
                    self._comprehension_observe(conn, row, lookup[0], word.correct, 'target', 'nl_en')
            document = grade.learning_document(row['answer_text'])
            capture = learning.Capture(row=row, context=document, provenance=policy)
            for idx, fix in enumerate(grade.vocabulary):
                error_id = conn.execute('INSERT INTO comprehension_errors(presentation_id,ordinal,body_json) VALUES(?,?,?)',
                                        (presentation_id, idx, fix.model_dump_json())).lastrowid
                for role in ('intended', 'confused'):
                    lexeme = getattr(fix, role)
                    if lexeme is None or not self._word_allowed(conn, 'nl', lexeme.lemma):
                        continue
                    lookup_id = self._learning_lookup(conn, fix, role, capture)
                    conn.execute('INSERT INTO comprehension_error_words VALUES(?,?,?)', (error_id, role, lookup_id))
                    self._comprehension_observe(conn, row, lookup_id, False, role, 'en_nl')
                    self._accretion_compound(conn, lookup_id)
            self._comprehension_replay(conn, presentation_id)
            self._comprehension_complete(conn)
        return self.comprehension_get(presentation_id)

    def comprehension_flag(self, passage_id: int, reason: str) -> None:
        """Quarantine a faulty passage while retaining real effort; e.g. its word failures no longer affect shared scheduling."""

        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('UPDATE comprehension_passages SET flagged_at=?,flag_reason=? WHERE id=?', (time.time(), reason, passage_id))
            ids = [row[0] for row in conn.execute('SELECT p.id FROM comprehension_presentations p JOIN comprehension_sessions s ON s.id=p.session_id WHERE s.passage_id=?', (passage_id,))]
            for pid in ids:
                conn.execute("UPDATE comprehension_presentations SET outcome='invalid',score=NULL WHERE id=?", (pid,))
                self._comprehension_replay(conn, pid)
            conn.execute("UPDATE comprehension_sessions SET status='stopped',finished_at=? WHERE passage_id=?", (time.time(), passage_id))
