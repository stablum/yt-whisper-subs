"""Persist approved sentence builds, resumable rounds and independent grammar results."""

import json
import random
import time
import uuid

from yt_whisper_subs import word_order


SCHEMA = """
CREATE TABLE IF NOT EXISTS word_order_questions (
    id INTEGER PRIMARY KEY,
    lookup_id INTEGER NOT NULL REFERENCES vocab_lookups(id),
    extras INTEGER NOT NULL CHECK(extras BETWEEN 0 AND 4),
    pattern TEXT NOT NULL,
    body_json TEXT NOT NULL,
    validation_json TEXT NOT NULL,
    model TEXT NOT NULL,
    reasoning TEXT NOT NULL,
    prompt_version INTEGER NOT NULL,
    created_at REAL NOT NULL,
    flagged_at REAL
);
CREATE INDEX IF NOT EXISTS idx_word_order_lookup ON word_order_questions(lookup_id,extras,flagged_at);
CREATE TABLE IF NOT EXISTS word_order_sessions (
    id TEXT PRIMARY KEY,
    options_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    finished_at REAL,
    status TEXT NOT NULL CHECK(status IN ('active','completed','stopped'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_word_order_active ON word_order_sessions(status) WHERE status='active';
CREATE TABLE IF NOT EXISTS word_order_presentations (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES word_order_sessions(id),
    question_id INTEGER NOT NULL REFERENCES word_order_questions(id),
    ordinal INTEGER NOT NULL,
    tray_json TEXT NOT NULL,
    draft_json TEXT NOT NULL DEFAULT '[]',
    shown_at REAL,
    answered_at REAL,
    elapsed_ms INTEGER,
    answer_json TEXT,
    outcome TEXT CHECK(outcome IN ('correct','wrong','invalid')),
    grade_json TEXT,
    viewed_at REAL,
    UNIQUE(session_id,ordinal),
    UNIQUE(session_id,question_id)
);
CREATE INDEX IF NOT EXISTS idx_word_order_answered ON word_order_presentations(answered_at);
CREATE INDEX IF NOT EXISTS idx_word_order_presented ON word_order_presentations(question_id);
"""

_PRESENTATION = """
SELECT p.*,q.body_json,q.validation_json,q.flagged_at,s.status
FROM word_order_presentations p JOIN word_order_questions q ON q.id=p.question_id
JOIN word_order_sessions s ON s.id=p.session_id
"""


class WordOrderDbMixin:
    """Store block identities and verified solutions, e.g. restarting never reshuffles a draft."""

    def word_order_stock(self, opts, policy: dict) -> list[dict]:
        """Prefer unseen approvals and also keep checked examples for a possible later review."""

        with self._connect() as conn:
            rows = conn.execute(
                "SELECT q.*,COUNT(p.shown_at) AS uses FROM word_order_questions q "
                "LEFT JOIN word_order_presentations p ON p.question_id=q.id WHERE q.flagged_at IS NULL "
                "AND q.extras=:extras AND q.model=:model AND q.reasoning=:reasoning AND q.prompt_version=:prompt_version "
                "GROUP BY q.id ORDER BY uses,q.id DESC", {**policy, "extras": opts.extras})
            return [dict(row) for row in rows]

    def word_order_cache(self, draft, verdict, target: dict, opts, policy: dict) -> dict:
        """Cache only independently verified bilingual builds linked to their exact vocabulary sighting."""

        draft.check(target["word_id"], opts.extras)
        verdict.check(draft)
        with self._connect() as conn:
            row = conn.execute("SELECT word_id FROM vocab_lookups WHERE id=?", (target["lookup_id"],)).fetchone()
            if row is None or row[0] != draft.word_id:
                raise ValueError("The source vocabulary identity changed")
            cursor = conn.execute(
                "INSERT INTO word_order_questions(lookup_id,extras,pattern,body_json,validation_json,model,reasoning,"
                "prompt_version,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (target["lookup_id"], opts.extras, draft.pattern, draft.model_dump_json(), verdict.model_dump_json(),
                 policy["model"], policy["reasoning"], policy["prompt_version"], time.time()))
            return dict(conn.execute("SELECT * FROM word_order_questions WHERE id=?", (cursor.lastrowid,)).fetchone())

    def word_order_create(self, opts, questions: list[dict], _scheduler: str) -> str:
        """Consume the standby atomically and shuffle the bank once before opening the round."""

        ids = [q["id"] for q in questions]
        if len(ids) != opts.count or len(set(ids)) != opts.count:
            raise ValueError("A sentence builder round requires its exact distinct question count")
        session_id, now = uuid.uuid4().hex, time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            displays = []
            for idx, question_id in enumerate(ids, 1):
                q = conn.execute("SELECT body_json,validation_json FROM word_order_questions WHERE id=? "
                                 "AND extras=? AND flagged_at IS NULL", (question_id, opts.extras)).fetchone()
                if q is None:
                    raise ValueError("A sentence builder question is no longer available")
                draft = word_order.Draft.model_validate_json(q["body_json"])
                verdict = word_order.Verdict.model_validate_json(q["validation_json"])
                tray = list(range(len(draft.bank)))
                random.shuffle(tray)
                while [block for block in tray if block < len(draft.tiles)] in verdict.orders:
                    random.shuffle(tray)
                displays.append((session_id, question_id, idx, json.dumps(tray)))
            self._word_order_close(conn, "stopped")
            self._practice_consume_stock(conn, "word_order")
            conn.execute("INSERT INTO word_order_sessions VALUES(?,?,?,NULL,'active')", (session_id, opts.model_dump_json(), now))
            conn.executemany("INSERT INTO word_order_presentations(session_id,question_id,ordinal,tray_json) VALUES(?,?,?,?)", displays)
        return session_id

    def word_order_active(self) -> str | None:
        """Read the active round without marking any question shown."""

        with self._connect() as conn:
            row = conn.execute("SELECT id FROM word_order_sessions WHERE status='active'").fetchone()
            return row[0] if row else None

    def word_order_options(self, session_id: str) -> word_order.Options:
        """Read the frozen request, e.g. editing next-round options cannot change a saved bank."""

        with self._connect() as conn:
            saved = conn.execute("SELECT options_json FROM word_order_sessions WHERE id=?", (session_id,)).fetchone()
        return word_order.Options.model_validate_json(saved[0])

    def word_order_resume(self) -> str | None:
        """Resume unfinished feedback as well as untouched questions."""

        with self._connect() as conn:
            row = conn.execute("SELECT s.id FROM word_order_sessions s WHERE s.status='active' OR EXISTS ("
                               "SELECT 1 FROM word_order_presentations p WHERE p.session_id=s.id "
                               "AND p.outcome IN ('correct','wrong') AND p.viewed_at IS NULL) "
                               "ORDER BY s.status='active' DESC,s.created_at DESC LIMIT 1").fetchone()
            return row[0] if row else None

    def word_order_current(self, session_id: str) -> dict | None:
        """Open the next question or resume its unreviewed correction."""

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(_PRESENTATION + " WHERE p.session_id=? AND q.flagged_at IS NULL AND p.outcome IS NOT 'invalid' "
                               "AND ((s.status='active' AND p.outcome IS NULL) OR (p.answer_json IS NOT NULL AND p.viewed_at IS NULL)) "
                               "ORDER BY p.ordinal LIMIT 1", (session_id,)).fetchone()
            if row is None:
                return None
            conn.execute("UPDATE word_order_presentations SET shown_at=COALESCE(shown_at,?) WHERE id=?", (time.time(), row["id"]))
            return dict(row)

    def word_order_draft(self, presentation_id: int, blocks: list[int]) -> None:
        """Keep the current arrangement on navigation and restart, e.g. a dragged tile remains in place."""

        with self._connect() as conn:
            conn.execute("UPDATE word_order_presentations SET draft_json=? WHERE id=? AND outcome IS NULL AND shown_at IS NOT NULL",
                         (json.dumps(blocks), presentation_id))

    def word_order_answer(self, presentation_id: int, blocks: list[int], elapsed_ms: int) -> dict:
        """Score and save one immutable submission, even if Check is clicked twice."""

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(_PRESENTATION + " WHERE p.id=?", (presentation_id,)).fetchone()
            if row is None or row["shown_at"] is None or row["flagged_at"] is not None:
                raise ValueError("This sentence builder question is unavailable")
            if row["outcome"] in ("correct", "wrong"):
                return dict(row)
            if row["status"] != "active" or row["outcome"] is not None or elapsed_ms < 0:
                raise ValueError("This sentence builder round has ended")
            draft = word_order.Draft.model_validate_json(row["body_json"])
            verdict = word_order.Verdict.model_validate_json(row["validation_json"])
            grade = word_order.grade(draft, verdict, blocks)
            conn.execute("UPDATE word_order_presentations SET answer_json=?,answered_at=?,elapsed_ms=?,outcome=?,grade_json=? WHERE id=?",
                         (json.dumps(blocks), time.time(), elapsed_ms, "correct" if grade.correct else "wrong",
                          grade.model_dump_json(), presentation_id))
            self._word_order_close(conn, "completed")
            return dict(conn.execute(_PRESENTATION + " WHERE p.id=?", (presentation_id,)).fetchone())

    def word_order_viewed(self, presentation_id: int) -> None:
        """Acknowledge feedback only on continuing, so closing the window can resume the correction."""

        with self._connect() as conn:
            conn.execute("UPDATE word_order_presentations SET viewed_at=COALESCE(viewed_at,?) WHERE id=? AND answer_json IS NOT NULL",
                         (time.time(), presentation_id))

    def word_order_report(self, presentation_id: int) -> None:
        """Retire a disputed exercise while preserving the learner's effort history."""

        with self._connect() as conn:
            row = conn.execute("SELECT question_id FROM word_order_presentations WHERE id=? AND shown_at IS NOT NULL",
                               (presentation_id,)).fetchone()
            if row:
                conn.execute("UPDATE word_order_questions SET flagged_at=COALESCE(flagged_at,?) WHERE id=?", (time.time(), row[0]))
                conn.execute("UPDATE word_order_presentations SET outcome='invalid' WHERE question_id=?", (row[0],))
                self._word_order_close(conn, "completed")

    @staticmethod
    def _word_order_close(conn, status: str) -> None:
        """Complete fully resolved rounds, or stop the old round when explicitly replaced."""

        ready = "" if status == "stopped" else (
            "AND NOT EXISTS (SELECT 1 FROM word_order_presentations p WHERE p.session_id=s.id AND p.outcome IS NULL)")
        conn.execute(f"UPDATE word_order_sessions AS s SET status=?,finished_at=? WHERE s.status='active' {ready}",
                     (status, time.time()))

    def word_order_summary(self, session_id: str) -> dict:
        """Supply the shared guided practice and scheduling contracts."""

        with self._connect() as conn:
            return dict(conn.execute(
                "SELECT COUNT(*) AS planned,COUNT(answer_json) AS submitted,COUNT(*) FILTER(WHERE outcome='correct') AS correct,"
                "COUNT(*) FILTER(WHERE outcome='wrong') AS wrong,COUNT(*) FILTER(WHERE outcome='invalid') AS invalid,"
                "COUNT(*) FILTER(WHERE outcome IN ('correct','wrong') AND viewed_at IS NULL) AS unseen "
                "FROM word_order_presentations WHERE session_id=?", (session_id,)).fetchone())

    def word_order_results(self) -> list[dict]:
        """Read non-disputed scores for grammar selection and date-based progress."""

        with self._connect() as conn:
            rows = conn.execute("SELECT p.answered_at,p.outcome,q.pattern,json_extract(p.grade_json,'$.partial') AS partial "
                                "FROM word_order_presentations p JOIN word_order_questions q ON q.id=p.question_id "
                                "WHERE p.outcome IN ('correct','wrong') ORDER BY p.answered_at")
            return [dict(row) for row in rows]
