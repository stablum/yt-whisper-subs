"""Persist the paced metadata queue as one focused database concern.

Example: `LibraryDb` composes `MetadataDbMixin` beside catalog methods.
"""

from __future__ import annotations

import time
from typing import NamedTuple

from yt_whisper_subs import cfg


class MetadataQueueState(NamedTuple):
    """Summarize ready and deferred metadata work from one database snapshot.

    Example: `state.retry_at` schedules the next cooled-down lookup exactly.
    """

    pending: int
    ready: int
    retry_at: int | None

    @property
    def deferred(self) -> int:
        """Count pending rows still inside their failed-lookup cooldown.

        Example: one pending row with no ready work yields `deferred == 1`.
        """

        return self.pending - self.ready


class MetadataDbMixin:
    """Add cooldown-aware metadata queue operations to a SQLite catalog owner.

    Example: `db.metadata_backfill_ids(1, retry_before)` selects one lookup.
    """

    def reserve_video_lookup(
        self,
        video_id: str,
        *,
        live_cooldown: int = 0,
        force: bool = False,
    ) -> int:
        """Pace automatic lookups while recording explicit forced checks too.

        Example: `force=True` bypasses waits but delays later queued metadata.
        """

        now = int(time.time())
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT v.live_retry_at,
                       (SELECT value FROM settings WHERE key='last_video_lookup_at') AS last_lookup_at
                FROM videos v WHERE v.video_id=?
                """,
                (video_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError(f"unknown video: {video_id}")
            last_lookup = int(row["last_lookup_at"] or 0)
            global_wait = max(0, last_lookup + cfg.DEFAULT_LIBRARY_METADATA_PACE_SECONDS - now)
            live_wait = max(0, int(row["live_retry_at"] or 0) - now) if live_cooldown else 0
            wait = max(global_wait, live_wait)
            if wait and not force:
                return wait
            conn.execute(
                """
                INSERT INTO settings(key, value) VALUES ('last_video_lookup_at', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (str(now),),
            )
            if live_cooldown:
                conn.execute(
                    "UPDATE videos SET live_retry_at=? WHERE video_id=?",
                    (now + live_cooldown, video_id),
                )
        return 0

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

    def metadata_queue_state(
        self,
        retry_seconds: int,
        now: int | None = None,
    ) -> MetadataQueueState:
        """Describe ready work and the earliest deferred retry in one query.

        Example: `db.metadata_queue_state(86_400)` distinguishes queued work.
        """

        current = now if now is not None else int(time.time())
        retry_before = current - retry_seconds
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS pending,
                    COALESCE(SUM(
                        metadata_attempted_at IS NULL OR metadata_attempted_at <= ?
                    ), 0) AS ready,
                    MIN(CASE
                        WHEN metadata_attempted_at > ?
                        THEN metadata_attempted_at + ?
                    END) AS retry_at
                FROM videos
                WHERE metadata_checked_at IS NULL
                """,
                (retry_before, retry_before, retry_seconds),
            ).fetchone()
        assert row is not None
        retry_at = int(row["retry_at"]) if row["retry_at"] is not None else None
        return MetadataQueueState(int(row["pending"]), int(row["ready"]), retry_at)

    def reset_metadata_attempts(self) -> int:
        """Release unresolved lookups after the user supplies new credentials.

        Example: selecting Firefox cookies makes deferred age-gated rows ready.
        """

        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE videos
                SET metadata_attempted_at=NULL
                WHERE metadata_checked_at IS NULL
                  AND metadata_attempted_at IS NOT NULL
                """
            )
        return cursor.rowcount
