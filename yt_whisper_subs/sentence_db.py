"""Durable sentence rounds and immutable bilingual exercise snapshots.

Example: a saved first attempt remains pending through a failed correction request or app restart.
"""

from __future__ import annotations

import json
import time
import uuid

from yt_whisper_subs import sentence_review_db
from yt_whisper_subs import vocabulary_db


PRESENTATION = """
SELECT p.*,q.body_json,q.direction,q.anchor_word_id,q.scene,q.flagged_at,s.status,s.scheduler_json,s.options_json
FROM sentence_presentations p JOIN sentence_questions q ON q.id=p.question_id
JOIN sentence_sessions s ON s.id=p.session_id
"""


class SentenceDbMixin(sentence_review_db.SentenceReviewDbMixin):
    """Own durable sentence lifecycle; e.g. resuming a pending answer needs no language-provider call."""

    def sentence_cached(self, plan: dict, policy: dict) -> list[dict]:
        """Reuse only the exact saved contexts and direction under the current generation policy; e.g. do not substitute a new word sense."""

        params = {"anchor": plan["anchor_word_id"], "scene": plan["scene"], "focus_key": self._sentence_key(plan), **policy}
        with self._connect() as conn:
            rows = conn.execute("SELECT q.*,COUNT(p.shown_at) AS uses FROM sentence_questions q "
                                "LEFT JOIN sentence_presentations p ON p.question_id=q.id "
                                "WHERE q.anchor_word_id=:anchor AND q.scene=:scene AND q.focus_key=:focus_key "
                                "AND q.direction=:direction AND q.model=:model AND q.reasoning=:reasoning "
                                "AND q.prompt_version=:prompt_version AND q.flagged_at IS NULL "
                                "GROUP BY q.id ORDER BY uses,q.id DESC", params)
            return [dict(row) for row in rows]

    def sentence_pool(self, opts, policy: dict) -> list[dict]:
        """Consume unseen approved pairs across random companion/context plans; e.g. reopening cannot strand a paid unused sentence."""

        params = {**policy, "scene": opts.scene, "words": opts.words}
        sql = f"""
        SELECT q.*,COUNT(p.shown_at) AS uses FROM sentence_questions q
        LEFT JOIN sentence_presentations p ON p.question_id=q.id
        WHERE q.flagged_at IS NULL AND q.direction=:direction AND q.model=:model
          AND q.reasoning=:reasoning AND q.prompt_version=:prompt_version
          AND (:scene='mixed' OR q.scene=:scene)
          AND (SELECT COUNT(*) FROM sentence_question_words w WHERE w.question_id=q.id)<=:words
          AND NOT EXISTS(SELECT 1 FROM sentence_question_words w WHERE w.question_id=q.id
                         AND w.lookup_id NOT IN (SELECT lookup_id FROM ({vocabulary_db.READY_WORDS})))
        GROUP BY q.id HAVING COUNT(p.shown_at)=0 ORDER BY q.id
        """
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(sql, params)]

    @staticmethod
    def _sentence_key(plan: dict) -> str:
        """Give a target context set one deterministic cache key; e.g. changed lookup meanings require a fresh bilingual pair."""

        return ":".join(str(value) for value in sorted(row["lookup_id"] for row in plan["words"]))

    def sentence_cache(self, draft, verdict, plan: dict, policy: dict) -> dict:
        """Persist an approved pair with normalized source references; e.g. generated IDs cannot attach it to unrelated clicks."""

        expected = {row["word_id"]: row["lookup_id"] for row in plan["words"]}
        draft.check(set(expected), "mixed" if plan["allow_context_change"] else plan["scene"])
        if not verdict.valid or verdict.anchor_word_id != draft.anchor_word_id or draft.anchor_word_id != plan["anchor_word_id"]:
            raise ValueError("Only approved sentence targets may be cached")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            actual = {row[0]: row[1] for row in conn.execute(
                "SELECT word_id,id FROM vocab_lookups WHERE id IN (SELECT value FROM json_each(?))",
                (json.dumps(list(expected.values())),))}
            if actual != expected:
                raise ValueError("Sentence source lookups changed or were substituted")
            cursor = conn.execute("INSERT INTO sentence_questions "
                                  "(direction,anchor_word_id,focus_key,scene,body_json,validation_json,model,reasoning,prompt_version,created_at) "
                                  "VALUES(?,?,?,?,?,?,?,?,?,?)", (policy["direction"], draft.anchor_word_id, self._sentence_key(plan),
                                                                 draft.scene, draft.model_dump_json(), verdict.model_dump_json(),
                                                                 policy["model"], policy["reasoning"], policy["prompt_version"], time.time()))
            question_id = cursor.lastrowid
            conn.executemany("INSERT INTO sentence_question_words VALUES(?,?,?)",
                             [(question_id, expected[row.word_id], idx) for idx, row in enumerate(draft.words)])
            return dict(conn.execute("SELECT * FROM sentence_questions WHERE id=?", (question_id,)).fetchone())

    def sentence_due(self, policy: dict, now: float) -> list[dict]:
        """Keep writing mistakes reviewable even when target meanings are known; e.g. a word-order error schedules its whole sentence."""

        with self._connect() as conn:
            rows = conn.execute("SELECT q.*,s.card_json,s.due_at FROM sentence_question_states s "
                                "JOIN sentence_questions q ON q.id=s.question_id WHERE s.due_at<=:now "
                                "AND q.flagged_at IS NULL AND q.direction=:direction AND q.model=:model "
                                "AND q.reasoning=:reasoning AND q.prompt_version=:prompt_version ORDER BY s.due_at,q.id",
                                {**policy, "now": now})
            return [dict(row) for row in rows]

    def sentence_create(self, opts, questions: list[dict], scheduler_json: str) -> str:
        """Replace a round only after preparation succeeds; e.g. a partial API failure leaves the old session resumable."""

        if len(questions) != opts.count or len({row["id"] for row in questions}) != len(questions):
            raise ValueError("A sentence round needs its requested number of distinct approved questions")
        session_id, now = uuid.uuid4().hex, time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for question in questions:
                row = conn.execute("SELECT direction,flagged_at FROM sentence_questions WHERE id=?", (question["id"],)).fetchone()
                if row is None or row["flagged_at"] is not None or row["direction"] != opts.direction.value:
                    raise ValueError("An exercise is unavailable for this translation direction")
            conn.execute("UPDATE sentence_sessions SET status='stopped',finished_at=? WHERE status='active'", (now,))
            conn.execute("INSERT INTO sentence_sessions VALUES(?,?,?,?,?,?,?)",
                         (session_id, opts.direction.value, opts.model_dump_json(), scheduler_json, now, None, "active"))
            conn.executemany("INSERT INTO sentence_presentations(session_id,question_id,ordinal) VALUES(?,?,?)",
                             [(session_id, row["id"], idx) for idx, row in enumerate(questions, 1)])
        return session_id

    @staticmethod
    def _sentence_row(row) -> dict | None:
        """Decode the original bilingual and grading snapshots; e.g. no re-generation occurs when resuming."""

        if row is None:
            return None
        result = dict(row)
        result["content"] = json.loads(result.pop("body_json"))
        result["grade"] = json.loads(result["grade_json"]) if result["grade_json"] else None
        return result

    def sentence_active(self) -> str | None:
        """Find a resumable session without counting exposure; e.g. viewing statistics stays read-only."""

        with self._connect() as conn:
            row = conn.execute("SELECT id FROM sentence_sessions WHERE status='active' ORDER BY created_at DESC LIMIT 1").fetchone()
        return row[0] if row else None

    def sentence_get(self, presentation_id: int) -> dict:
        """Read a durable submission without modifying it; e.g. retry only a failed correction."""

        with self._connect() as conn:
            row = conn.execute(PRESENTATION + " WHERE p.id=?", (presentation_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown sentence exercise")
        return self._sentence_row(row)

    def sentence_current(self, session_id: str) -> dict | None:
        """Timestamp only the next visible unresolved question; e.g. an API response on another tab adds no exposure."""

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(PRESENTATION + " WHERE p.session_id=? AND p.outcome IS NULL AND s.status='active' "
                               "ORDER BY p.ordinal LIMIT 1", (session_id,)).fetchone()
            if row is None:
                return None
            conn.execute("UPDATE sentence_presentations SET shown_at=COALESCE(shown_at,?) WHERE id=?", (time.time(), row["id"]))
            return self._sentence_row(conn.execute(PRESENTATION + " WHERE p.id=?", (row["id"],)).fetchone())

    def sentence_submit(self, presentation_id: int, answer: str, elapsed_ms: int) -> dict:
        """Commit the exact first attempt before remote correction; e.g. double Enter cannot duplicate answers or lose a draft on timeout."""

        if not isinstance(answer, str) or not answer.strip() or len(answer) > 2000:
            raise ValueError("Type a sentence of at most 2,000 characters")
        if type(elapsed_ms) is not int or elapsed_ms < 0:
            raise ValueError("Invalid answer duration")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(PRESENTATION + " WHERE p.id=?", (presentation_id,)).fetchone()
            if row is None or row["shown_at"] is None:
                raise ValueError("This sentence has not been shown")
            if row["answer_text"] is not None or row["outcome"] is not None:
                return self._sentence_row(row)
            if row["status"] != "active":
                raise ValueError("This sentence round has ended")
            conn.execute("UPDATE sentence_presentations SET answer_text=?,answered_at=?,elapsed_ms=? WHERE id=?",
                         (answer, time.time(), elapsed_ms, presentation_id))
            return self._sentence_row(conn.execute(PRESENTATION + " WHERE p.id=?", (presentation_id,)).fetchone())

    def sentence_summary(self, session_id: str) -> dict:
        """Keep planned, shown, submitted and graded counts separate; e.g. pending corrections never become failures."""

        with self._connect() as conn:
            return dict(conn.execute("SELECT COUNT(*) AS planned,COUNT(shown_at) AS shown,COUNT(answer_text) AS submitted,"
                                     "COALESCE(SUM(outcome='correct'),0) AS correct,COALESCE(SUM(outcome='wrong'),0) AS wrong,"
                                     "COALESCE(SUM(outcome='invalid'),0) AS invalid FROM sentence_presentations WHERE session_id=?",
                                     (session_id,)).fetchone())

    def sentence_finish(self, session_id: str) -> dict:
        """Store partial results without inventing failures for unseen questions; e.g. End round differs from simply pausing."""

        with self._connect() as conn:
            self._sentence_complete(conn)
            conn.execute("UPDATE sentence_sessions SET status='stopped',finished_at=? WHERE id=? AND status='active'",
                         (time.time(), session_id))
        return self.sentence_summary(session_id)
