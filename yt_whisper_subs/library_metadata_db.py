"""Persist the paced metadata queue as one focused database concern.

Example: `LibraryDb` composes `MetadataDbMixin` beside catalog methods.
"""

from __future__ import annotations

import time


class MetadataDbMixin:
    """Add cooldown-aware metadata queue operations to a SQLite catalog owner.

    Example: `db.metadata_backfill_ids(1, retry_before)` selects one lookup.
    """

    def metadata_backfill_ids(self, limit: int, retry_before: int) -> list[str]:
        """Select a fair metadata work slice without retrying cooled-down rows.

        Example: `db.metadata_backfill_ids(1, retry_before)` returns one ID.
        """

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT v.video_id
                FROM videos v LEFT JOIN media m USING(video_id)
                WHERE v.metadata_checked_at IS NULL
                  AND (
                      v.metadata_attempted_at IS NULL
                      OR v.metadata_attempted_at <= ?
                  )
                ORDER BY
                    v.metadata_attempted_at IS NOT NULL,
                    m.video_id IS NOT NULL DESC,
                    COALESCE(v.metadata_attempted_at, 0),
                    v.discovered_at DESC
                LIMIT ?
                """,
                (retry_before, limit),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def mark_metadata_attempt(self, video_id: str, attempted_at: int | None = None) -> None:
        """Move one lookup to the queue tail and start its retry cooldown.

        Example: `db.mark_metadata_attempt(video_id)` precedes network access.
        """

        timestamp = attempted_at if attempted_at is not None else int(time.time())
        with self._connect() as conn:
            conn.execute(
                "UPDATE videos SET metadata_attempted_at=? WHERE video_id=?",
                (timestamp, video_id),
            )

    def metadata_backlog_count(self) -> int:
        """Count records still awaiting a definitive metadata lookup.

        Example: `db.metadata_backlog_count()` feeds the GUI queue indicator.
        """

        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM videos WHERE metadata_checked_at IS NULL"
            ).fetchone()
        assert row is not None
        return int(row[0])
