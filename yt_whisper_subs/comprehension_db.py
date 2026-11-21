"""Persist reusable passages and resumable comprehension sessions.

Example: saving a draft before its audit lets a later retry reuse generation tokens.
"""

import json
import time
import uuid

import numpy as np

from yt_whisper_subs import comprehension as comp
from yt_whisper_subs import comprehension_schema as schema
from yt_whisper_subs import comprehension_review_db


class ComprehensionDbMixin(comprehension_review_db.ComprehensionReviewDbMixin):
    """Keep linguistic work outside transactions; e.g. another mode remains usable during passage generation."""

    def comprehension_pool(self, opts, policy: dict) -> dict | None:
        """Find unseen approved passages with enough questions, or an unfinished draft of the exact shape."""

        params = {**policy, 'exact': opts.content_key(), 'passage': opts.content_key(include_counts=False),
                  'mc': opts.mc, 'open': opts.open, 'direction': opts.direction.value}
        with self._connect() as conn:
            row = conn.execute(
                "SELECT t.* FROM comprehension_passages t WHERE model=:model AND reasoning=:reasoning AND version=:version "
                "AND flagged_at IS NULL AND error='' AND (content_key=:exact OR "
                "(validation_json IS NOT NULL AND json_remove(content_key,'$.mc','$.open')=json(:passage) "
                "AND json_extract(content_key,'$.mc')>=:mc AND json_extract(content_key,'$.open')>=:open)) "
                "AND NOT EXISTS(SELECT 1 FROM comprehension_sessions s WHERE s.passage_id=t.id "
                "AND (s.shown_at IS NOT NULL OR s.status='active' OR (s.status='ready' AND s.direction<>:direction))) "
                "ORDER BY validation_json IS NULL,content_key<>:exact,id LIMIT 1", params).fetchone()
            return dict(row) if row else None

    def comprehension_cache(self, passage: comp.Passage, opts, targets: list[dict], policy: dict,
                            *, replaces: tuple[int, str] | None = None) -> int:
        """Checkpoint generated text before independent review; e.g. a reviewer outage leaves a reusable draft."""

        with self._connect() as conn:
            cursor = conn.execute("INSERT INTO comprehension_passages(content_key,body_json,targets_json,model,reasoning,version,created_at) "
                                  "VALUES(?,?,?,?,?,?,?)", (opts.content_key(), passage.model_dump_json(), json.dumps(targets),
                                                          policy['model'], policy['reasoning'], policy['version'], time.time()))
            if replaces:
                old_id, reason = replaces
                conn.execute("UPDATE comprehension_passages SET error=? WHERE id=? AND validation_json IS NULL",
                             (reason[:3000], old_id))
            return cursor.lastrowid

    def comprehension_passage(self, passage_id: int) -> dict:
        """Read private source material for its own exercise; e.g. the listening view decides when the transcript may be rendered."""

        with self._connect() as conn:
            row = dict(conn.execute("SELECT * FROM comprehension_passages WHERE id=?", (passage_id,)).fetchone())
        row['content'] = json.loads(row['body_json'])
        return row

    def comprehension_approve(self, passage_id: int, verdict: comp.Audit) -> None:
        """Publish a completely audited set and shuffle its choices once; e.g. reopening never changes an answer's button positions."""

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM comprehension_passages WHERE id=?", (passage_id,)).fetchone()
            if row['validation_json'] is not None:
                return
            passage = comp.Passage.model_validate_json(row['body_json'])
            candidates = {t['word_id']: t['lookup_id'] for t in json.loads(row['targets_json'])}
            passage.check(comp.Options.model_validate_json(row['content_key']), set(candidates))
            unsupported = verdict.check(passage)
            rng, orders = np.random.default_rng(), {}
            for q in passage.questions:
                q.word_ids = [wid for wid in q.word_ids if wid not in unsupported[q.key]]
                if q.kind == 'mc':
                    order = rng.permutation(4).tolist()
                    orders[q.key] = order
                    q.choices = [q.choices[idx] for idx in order]
                    q.correct_idx = order.index(q.correct_idx)
                conn.execute("INSERT INTO comprehension_questions(passage_id,ordinal,kind,body_json) VALUES(?,?,?,?)",
                             (passage_id, q.key, q.kind, q.model_dump_json()))
            conn.executemany("INSERT INTO comprehension_targets VALUES(?,?,?)",
                             [(passage_id, t.word_id, candidates[t.word_id]) for t in passage.targets])
            audit = dict(verdict=verdict.model_dump(), choice_orders=orders)
            conn.execute("UPDATE comprehension_passages SET body_json=?,validation_json=?,error='' WHERE id=?",
                         (passage.model_dump_json(), json.dumps(audit), passage_id))

    def comprehension_reject(self, passage_id: int, error: str) -> None:
        """Retain rejected evidence without offering it again; e.g. repair generation can inspect the exact ambiguity."""

        with self._connect() as conn:
            conn.execute("UPDATE comprehension_passages SET error=? WHERE id=? AND validation_json IS NULL AND error=''",
                         (error[:3000], passage_id))

    def comprehension_recent(self, limit: int = 5) -> list[dict]:
        """Guide variety without replaying old passages; e.g. a weak word appears in another situation next time."""

        with self._connect() as conn:
            return [dict(row) for row in conn.execute("SELECT json_extract(t.body_json,'$.title') AS title,"
                        "json_extract(t.body_json,'$.genre') AS genre FROM comprehension_passages t "
                        "WHERE t.validation_json IS NOT NULL AND t.flagged_at IS NULL "
                        "ORDER BY t.id DESC LIMIT ?", (limit,))]

    def comprehension_active(self, direction: str) -> str | None:
        """Restore each modality independently; e.g. a half-finished listening round survives reading practice."""

        with self._connect() as conn:
            row = conn.execute("SELECT id FROM comprehension_sessions WHERE direction=? AND status='active'", (direction,)).fetchone()
            return row[0] if row else None

    def comprehension_resume(self, direction: str) -> str | None:
        """Restore active work before unseen feedback; e.g. grading completed while the window was closed still gets reviewed."""

        with self._connect() as conn:
            row = conn.execute("SELECT s.id FROM comprehension_sessions s WHERE direction=? AND (status='active' OR EXISTS "
                               "(SELECT 1 FROM comprehension_presentations p WHERE p.session_id=s.id AND p.answer_text IS NOT NULL "
                               "AND p.viewed_at IS NULL AND p.outcome IN ('correct','wrong'))) ORDER BY status='active' DESC,created_at DESC LIMIT 1", (direction,)).fetchone()
            return row[0] if row else None

    def comprehension_ready(self, opts, policy: dict) -> dict | None:
        """Reuse an unopened reserved passage for matching content; e.g. changing narrator does not purchase another story."""

        with self._connect() as conn:
            row = conn.execute("SELECT s.* FROM comprehension_sessions s JOIN comprehension_passages t ON t.id=s.passage_id "
                               "WHERE s.direction=? AND s.status='ready' AND json_remove(t.content_key,'$.mc','$.open')=json(?) "
                               "AND json_extract(s.options_json,'$.mc')=? AND json_extract(s.options_json,'$.open')=? AND t.model=? AND t.reasoning=? "
                               "AND t.version=? AND t.flagged_at IS NULL ORDER BY s.created_at LIMIT 1",
                               (opts.direction.value, opts.content_key(include_counts=False), opts.mc, opts.open,
                                policy['model'], policy['reasoning'], policy['version'])).fetchone()
            return dict(row) if row else None

    def comprehension_session(self, session_id: str) -> dict:
        """Read the frozen round request; e.g. subsequent option edits affect only the next round."""

        with self._connect() as conn:
            return dict(conn.execute("SELECT * FROM comprehension_sessions WHERE id=?", (session_id,)).fetchone())

    def comprehension_create(self, passage_id: int, opts, scheduler: str, *, background=False) -> str:
        """Replace an active round only after content and media succeed; e.g. a failed new round preserves the previous draft."""

        session_id = uuid.uuid4().hex
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT validation_json,flagged_at FROM comprehension_passages WHERE id=?', (passage_id,)).fetchone()
            if row is None or row['validation_json'] is None or row['flagged_at'] is not None:
                raise ValueError('Deze tekst is niet goedgekeurd.')
            questions = conn.execute('SELECT id,kind FROM comprehension_questions WHERE passage_id=? ORDER BY ordinal', (passage_id,))
            remaining, selected = {'mc': opts.mc, 'open': opts.open}, []
            for question in questions:
                kind = question['kind']
                if remaining[kind]:
                    remaining[kind] -= 1
                    selected.append(question['id'])
            if any(remaining.values()):
                raise ValueError('De ronde bevat niet het gevraagde aantal vragen.')
            now = time.time()
            if not background:
                conn.execute("UPDATE comprehension_sessions SET status='stopped',finished_at=? WHERE direction=? AND status='active'", (now, opts.direction.value))
            conn.execute("UPDATE comprehension_sessions SET status='stopped',finished_at=? "
                         "WHERE direction=? AND passage_id=? AND status='ready'", (now, opts.direction.value, passage_id))
            conn.execute('INSERT INTO comprehension_sessions VALUES(?,?,?,?,?,?,NULL,NULL,?)',
                         (session_id, passage_id, opts.direction.value, opts.model_dump_json(), scheduler, now, 'ready' if background else 'active'))
            conn.executemany('INSERT INTO comprehension_presentations(session_id,question_id,ordinal) VALUES(?,?,?)',
                             [(session_id, question_id, ordinal) for ordinal, question_id in enumerate(selected, 1)])
        return session_id

    def comprehension_use_ready(self, session_id: str, opts) -> str:
        """Consume already paid content atomically; e.g. one standby session cannot be offered twice."""

        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute("UPDATE comprehension_sessions SET status='stopped',finished_at=? WHERE direction=? AND status='active'", (time.time(), opts.direction.value))
            cursor = conn.execute("UPDATE comprehension_sessions SET status='active',options_json=? WHERE id=? AND status='ready'", (opts.model_dump_json(), session_id))
            if cursor.rowcount != 1:
                raise ValueError('Deze voorbereide ronde is al geopend.')
        return session_id

    @staticmethod
    def _comprehension_row(row) -> dict:
        """Keep raw database fields and parsed private payloads distinct; e.g. renderers receive only fields they deliberately display."""

        result = dict(row)
        result['content'] = json.loads(result['body_json'])
        result['grade'] = json.loads(result['grade_json']) if result['grade_json'] else None
        return result

    def comprehension_get(self, presentation_id: int) -> dict:
        """Read one saved attempt without exposing a new question; e.g. a correction worker cannot increment presentation counts."""

        with self._connect() as conn:
            row = conn.execute(schema.PRESENTATION + ' WHERE p.id=?', (presentation_id,)).fetchone()
            return self._comprehension_row(row)

    def comprehension_history(self, session_id: str) -> list[dict]:
        """Supply a compact review rail; e.g. unseen corrections remain visible after all answers are submitted."""

        with self._connect() as conn:
            rows = conn.execute(schema.PRESENTATION + ' WHERE p.session_id=? ORDER BY p.ordinal', (session_id,))
            return [self._comprehension_row(row) for row in rows]

    def comprehension_open(self, presentation_id: int) -> dict:
        """Mark only an actually visible question shown; e.g. background preparation creates no learning exposure."""

        with self._connect() as conn:
            row = conn.execute(schema.PRESENTATION + ' WHERE p.id=?', (presentation_id,)).fetchone()
            if row is None or row['flagged_at'] is not None or (row['status'] != 'active' and row['shown_at'] is None):
                raise ValueError('Deze vraag is niet beschikbaar.')
            now = time.time()
            conn.execute('UPDATE comprehension_presentations SET shown_at=COALESCE(shown_at,?) WHERE id=?', (now, presentation_id))
            conn.execute('UPDATE comprehension_sessions SET shown_at=COALESCE(shown_at,?) WHERE id=?', (now, row['session_id']))
        return self.comprehension_get(presentation_id)

    def comprehension_summary(self, session_id: str) -> dict:
        """Keep submission, completion and partial understanding separate; e.g. pending grading does not count as failure."""

        with self._connect() as conn:
            return dict(conn.execute("SELECT COUNT(*) AS planned,COUNT(answer_text) AS submitted,COUNT(score) AS graded,"
                        "COALESCE(SUM(outcome='correct'),0) AS correct,COALESCE(SUM(outcome='wrong'),0) AS wrong,"
                        "COALESCE(SUM(outcome='invalid'),0) AS invalid,AVG(score) AS score,"
                        "COALESCE(SUM(grade_json IS NOT NULL AND viewed_at IS NULL AND outcome<>'invalid'),0) AS unseen "
                        "FROM comprehension_presentations WHERE session_id=?", (session_id,)).fetchone())

    def comprehension_draft(self, presentation_id: int) -> dict:
        """Restore unsent Dutch text and real editing time; e.g. visiting Activity never clears an open answer."""

        with self._connect() as conn:
            row = conn.execute('SELECT text,elapsed_ms FROM comprehension_drafts WHERE presentation_id=?', (presentation_id,)).fetchone()
            return dict(row) if row else dict(text='', elapsed_ms=0)

    def comprehension_save_draft(self, presentation_id: int, text: str, elapsed_ms: int) -> None:
        """Write only an unanswered visible draft; e.g. delayed autosave cannot overwrite the immutable first submission."""

        with self._connect() as conn:
            conn.execute("INSERT INTO comprehension_drafts SELECT p.id,?,? FROM comprehension_presentations p "
                         "JOIN comprehension_sessions s ON s.id=p.session_id WHERE p.id=? AND p.answer_text IS NULL "
                         "AND p.shown_at IS NOT NULL AND s.status='active' ON CONFLICT(presentation_id) DO UPDATE "
                         "SET text=excluded.text,elapsed_ms=excluded.elapsed_ms", (text, max(0, elapsed_ms), presentation_id))

    def comprehension_seen(self, presentation_id: int) -> None:
        """Record consulted feedback once; e.g. a queued response arriving on another tab remains unseen."""

        with self._connect() as conn:
            conn.execute("UPDATE comprehension_presentations SET viewed_at=COALESCE(viewed_at,?) "
                         "WHERE id=? AND grade_json IS NOT NULL AND outcome IN ('correct','wrong')", (time.time(), presentation_id))

    def comprehension_finish(self, session_id: str) -> None:
        """Stop future submissions without deleting history; e.g. starting another passage retains earlier corrections."""

        with self._connect() as conn:
            conn.execute("UPDATE comprehension_sessions SET status='stopped',finished_at=? WHERE id=? AND status='active'", (time.time(), session_id))
