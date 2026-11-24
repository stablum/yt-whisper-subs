"""Persist drafts and review history for asynchronously checked written practice.

Example: closing Verb Transformations keeps queued answers and resumes unread corrections.
"""

import time


_KINDS = ("sentence", "verb")
SCHEMA = "\n".join(f"""
CREATE TABLE IF NOT EXISTS {kind}_checks (
    presentation_id INTEGER PRIMARY KEY REFERENCES {kind}_presentations(id),
    state TEXT NOT NULL CHECK(state IN ('queued','checking','failed','done')),
    owner TEXT,
    lease_until REAL NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS {kind}_drafts (
    presentation_id INTEGER PRIMARY KEY REFERENCES {kind}_presentations(id),
    text TEXT NOT NULL,
    elapsed_ms INTEGER NOT NULL CHECK(elapsed_ms>=0)
);
CREATE TABLE IF NOT EXISTS {kind}_feedback_views (
    presentation_id INTEGER PRIMARY KEY REFERENCES {kind}_presentations(id),
    viewed_at REAL NOT NULL
);
""" for kind in _KINDS)


def enqueue(conn, kind: str, presentation_id: int) -> None:
    """Clear the draft and queue correction in the submission transaction."""

    conn.execute(f"DELETE FROM {kind}_drafts WHERE presentation_id=?", (presentation_id,))
    conn.execute(f"INSERT OR IGNORE INTO {kind}_checks(presentation_id,state) VALUES(?,'queued')", (presentation_id,))


class WrittenReviewDbMixin:
    """Expose a round's saved answers, drafts and correction status to the review sidebar."""

    def written_history(self, kind: str, session_id: str) -> list[dict]:
        """Read a compact outline without changing which questions have been shown."""

        # Verb feedback delivered before this queue was introduced is already part of past practice.
        viewed = ("CASE WHEN c.presentation_id IS NULL THEN p.graded_at ELSE v.viewed_at END"
                  if kind == "verb" else "v.viewed_at")
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT p.id,p.ordinal,p.shown_at,p.answer_text,p.outcome,c.state,c.error,"
                f"s.status AS session_status,{viewed} AS viewed_at "
                f"FROM {kind}_presentations p JOIN {kind}_sessions s ON s.id=p.session_id "
                f"LEFT JOIN {kind}_checks c ON c.presentation_id=p.id "
                f"LEFT JOIN {kind}_feedback_views v ON v.presentation_id=p.id "
                "WHERE p.session_id=? ORDER BY p.ordinal", (session_id,))
            return [dict(row) for row in rows]

    def written_seen(self, kind: str, presentation_id: int) -> float | None:
        """Acknowledge visible, usable feedback once, such as a corrected transformation."""

        with self._connect() as conn:
            conn.execute(f"INSERT OR IGNORE INTO {kind}_feedback_views SELECT id,? FROM {kind}_presentations "
                         "WHERE id=? AND outcome IN ('correct','wrong') AND grade_json IS NOT NULL",
                         (time.time(), presentation_id))
            row = conn.execute(f"SELECT viewed_at FROM {kind}_feedback_views WHERE presentation_id=?",
                               (presentation_id,)).fetchone()
            return row[0] if row else None

    def written_unseen_session(self, track) -> str | None:
        """Find the latest round with outstanding corrections ahead of unused stock."""

        kind = track.kind
        column, value = track.session_filter
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT s.id FROM {kind}_sessions s WHERE s.{column}=? AND EXISTS "
                f"(SELECT 1 FROM {kind}_presentations p JOIN {kind}_checks c ON c.presentation_id=p.id "
                f"LEFT JOIN {kind}_feedback_views v ON v.presentation_id=p.id WHERE p.session_id=s.id "
                "AND (p.outcome IS NULL OR (p.outcome IN ('correct','wrong') AND v.viewed_at IS NULL))) "
                "ORDER BY s.created_at DESC LIMIT 1", (value,)).fetchone()
            return row[0] if row else None

    def written_open(self, kind: str, presentation_id: int) -> dict:
        """Mark only the selected question as shown, without counting another exposure on review."""

        with self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE {kind}_presentations SET shown_at=COALESCE(shown_at,?) WHERE id=? "
                f"AND (shown_at IS NOT NULL OR session_id IN (SELECT id FROM {kind}_sessions WHERE status='active'))",
                (time.time(), presentation_id))
            if not cursor.rowcount:
                raise ValueError("This exercise is not available")
        return getattr(self, f"{kind}_get")(presentation_id)

    def written_draft(self, kind: str, presentation_id: int) -> dict:
        """Restore draft text and time spent editing, for example after reviewing an earlier answer."""

        with self._connect() as conn:
            row = conn.execute(f"SELECT text,elapsed_ms FROM {kind}_drafts WHERE presentation_id=?",
                               (presentation_id,)).fetchone()
            return dict(row) if row else {"text": "", "elapsed_ms": 0}

    def written_save_draft(self, kind: str, presentation_id: int, text: str, elapsed_ms: int) -> None:
        """Save only active, editable exercises so a late timer cannot alter a submission."""

        with self._connect() as conn:
            conn.execute(f"INSERT INTO {kind}_drafts SELECT p.id,?,? FROM {kind}_presentations p "
                         f"JOIN {kind}_sessions s ON s.id=p.session_id WHERE p.id=? AND p.shown_at IS NOT NULL "
                         "AND p.answer_text IS NULL AND p.outcome IS NULL AND s.status='active' "
                         "ON CONFLICT(presentation_id) DO UPDATE SET text=excluded.text,elapsed_ms=excluded.elapsed_ms",
                         (text, max(0, elapsed_ms), presentation_id))
