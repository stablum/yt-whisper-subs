"""Persist sentence correction jobs and unfinished drafts independently of a window.

Example: closing practice keeps queued answers and reclaims a crashed worker on the next launch.
"""

import time


SCHEMA = """
CREATE TABLE IF NOT EXISTS sentence_checks (
    presentation_id INTEGER PRIMARY KEY REFERENCES sentence_presentations(id),
    state TEXT NOT NULL CHECK(state IN ('queued','checking','failed','done')),
    owner TEXT,
    lease_until REAL NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS sentence_drafts (
    presentation_id INTEGER PRIMARY KEY REFERENCES sentence_presentations(id),
    text TEXT NOT NULL,
    elapsed_ms INTEGER NOT NULL CHECK(elapsed_ms>=0)
);
CREATE TABLE IF NOT EXISTS sentence_feedback_views (
    presentation_id INTEGER PRIMARY KEY REFERENCES sentence_presentations(id),
    viewed_at REAL NOT NULL
);
"""


class SentenceQueueDbMixin:
    """Keep queue transitions atomic with immutable answers; e.g. a repeated submit cannot create another paid job."""

    def sentence_history(self, session_id: str) -> list[dict]:
        """Read a compact outline without exposing questions; e.g. the rail shows pending and graded answers across a whole round."""

        with self._connect() as conn:
            rows = conn.execute("SELECT p.id,p.ordinal,p.shown_at,p.answer_text,p.outcome,c.state,c.error,s.status AS session_status,v.viewed_at "
                                "FROM sentence_presentations p JOIN sentence_sessions s ON s.id=p.session_id "
                                "LEFT JOIN sentence_checks c ON c.presentation_id=p.id "
                                "LEFT JOIN sentence_feedback_views v ON v.presentation_id=p.id "
                                "WHERE p.session_id=? ORDER BY p.ordinal", (session_id,))
            return [dict(row) for row in rows]

    def sentence_seen(self, presentation_id: int) -> float | None:
        """Record only visible, usable feedback, once; e.g. opening a pending answer or statistics cannot mark a correction consulted."""

        with self._connect() as conn:
            conn.execute("INSERT OR IGNORE INTO sentence_feedback_views SELECT id,? FROM sentence_presentations "
                         "WHERE id=? AND outcome IN ('correct','wrong') AND grade_json IS NOT NULL", (time.time(), presentation_id))
            row = conn.execute("SELECT viewed_at FROM sentence_feedback_views WHERE presentation_id=?", (presentation_id,)).fetchone()
            return row[0] if row else None

    def sentence_unseen_session(self, direction: str) -> str | None:
        """Recover unread feedback ahead of unused stock; e.g. app restart cannot strand a completed round's unseen corrections."""

        with self._connect() as conn:
            row = conn.execute("SELECT s.id FROM sentence_sessions s WHERE s.direction=? AND EXISTS(SELECT 1 FROM sentence_presentations p "
                               "WHERE p.session_id=s.id AND p.outcome IN ('correct','wrong') "
                               "AND NOT EXISTS(SELECT 1 FROM sentence_feedback_views v WHERE v.presentation_id=p.id)) "
                               "ORDER BY s.created_at DESC LIMIT 1", (direction,)).fetchone()
            return row[0] if row else None

    def sentence_open(self, presentation_id: int) -> dict:
        """Mark only the explicitly opened question visible; e.g. revisiting a corrected answer never increments exposure."""

        from yt_whisper_subs import sentence_db

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(sentence_db.PRESENTATION + " WHERE p.id=?", (presentation_id,)).fetchone()
            if row is None or (row["status"] != "active" and row["shown_at"] is None):
                raise ValueError("This sentence is not available")
            conn.execute("UPDATE sentence_presentations SET shown_at=COALESCE(shown_at,?) WHERE id=?", (time.time(), presentation_id))
        return self.sentence_get(presentation_id)

    def sentence_draft(self, presentation_id: int) -> dict:
        """Restore draft text and actual editing time; e.g. time spent reviewing another answer is excluded."""

        with self._connect() as conn:
            row = conn.execute("SELECT text,elapsed_ms FROM sentence_drafts WHERE presentation_id=?", (presentation_id,)).fetchone()
            return dict(row) if row else {"text": "", "elapsed_ms": 0}

    def sentence_save_draft(self, presentation_id: int, text: str, elapsed_ms: int) -> None:
        """Autosave only unanswered active exercises; e.g. a late timer cannot modify a submitted first attempt."""

        with self._connect() as conn:
            conn.execute("INSERT INTO sentence_drafts SELECT p.id,?,? FROM sentence_presentations p "
                         "JOIN sentence_sessions s ON s.id=p.session_id WHERE p.id=? AND p.shown_at IS NOT NULL "
                         "AND p.answer_text IS NULL AND p.outcome IS NULL AND s.status='active' "
                         "ON CONFLICT(presentation_id) DO UPDATE SET text=excluded.text,elapsed_ms=excluded.elapsed_ms",
                         (text, max(0, elapsed_ms), presentation_id))
