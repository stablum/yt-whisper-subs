"""Persist crash-recoverable pipeline jobs as one focused database concern.

Example: `LibraryDb` composes `PipelineJobDbMixin` beside its catalog methods.
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Iterable

import psutil

from yt_whisper_subs import library_types as types
from yt_whisper_subs import pipeline_progress as progress


_EXTRA_COLUMNS = {
    "owner_pid": "INTEGER",
    "owner_started_at": "REAL",
    "requested_ns": "INTEGER",
    "force_live_check": "INTEGER NOT NULL DEFAULT 0",
    "retry_at": "INTEGER",
    "failures": "INTEGER NOT NULL DEFAULT 0",
}
SCHEMA = f"""
CREATE TABLE IF NOT EXISTS pipeline_jobs (
    video_id TEXT PRIMARY KEY REFERENCES videos(video_id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    stage TEXT NOT NULL,
    fraction REAL NOT NULL DEFAULT 0,
    label TEXT NOT NULL,
    started_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    {", ".join(f"{name} {spec}" for name, spec in _EXTRA_COLUMNS.items())}
);
"""
_WAITING = (types.PipelineJobState.QUEUED, types.PipelineJobState.INTERRUPTED)


def upgrade(conn: sqlite3.Connection) -> None:
    """Extend existing journals in place, e.g. keep the stage saved by v0.6.37."""

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(pipeline_jobs)")}
    for name, spec in _EXTRA_COLUMNS.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE pipeline_jobs ADD COLUMN {name} {spec}")


def _owner_is_alive(row: sqlite3.Row) -> bool:
    """Distinguish a live owner from a dead process or a reused process ID.

    Example: a second catalog client cannot interrupt the first client's job.
    """

    if row["owner_pid"] is None or row["owner_started_at"] is None:
        return False
    try:
        owner = psutil.Process(int(row["owner_pid"]))
        return owner.is_running() and owner.create_time() == row["owner_started_at"]
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        # An inaccessible process is not evidence of a crash.
        return True


class PipelineJobDbMixin:
    """Add durable pipeline checkpoints to a SQLite catalog owner.

    Example: `db.recover_pipeline_jobs()` finds work left by a system crash.
    """

    def queue_pipeline_jobs(
        self,
        video_ids: Iterable[str],
        kind: types.PipelineKind,
        *,
        force_live_check: bool = False,
    ) -> list[str]:
        """Commit new work in order, e.g. promote a batch of automatic downloads."""

        with self._connect() as conn:
            return self._insert_pipeline_jobs(conn, video_ids, kind, force_live_check=force_live_check)

    def _queue_snapshot_downloads(
        self, conn: sqlite3.Connection, channel_id: int, video_ids: list[str], new_ids: set[str],
    ) -> list[str]:
        """Commit auto-download intent with discovery, e.g. release an ended stream."""

        candidates = {
            row["video_id"]: row["live_status"]
            for row in conn.execute(
                """
                SELECT v.video_id, v.live_status, v.auto_pending
                FROM videos v LEFT JOIN media m USING(video_id) LEFT JOIN video_history h USING(video_id)
                WHERE v.subscription_id=? AND m.video_id IS NULL AND COALESCE(h.files_removed, 0)=0
                """,
                (channel_id,),
            )
            if row["auto_pending"] or row["video_id"] in new_ids
        }
        blocked = []
        ready = []
        for video_id in dict.fromkeys(video_ids):
            if video_id in candidates:
                (blocked if candidates[video_id] in types.LIVE_BLOCKED else ready).append(video_id)
        conn.executemany("UPDATE videos SET auto_pending=1 WHERE video_id=?", ((vid,) for vid in blocked))
        return self._insert_pipeline_jobs(conn, ready, types.PipelineKind.DOWNLOAD, automatic=True)

    def set_auto_pending(self, video_ids: Iterable[str], pending: bool) -> None:
        """Remember eligible new streams until a later channel snapshot ends them.

        Example: an initially live auto-download candidate waits for `was_live`.
        """

        with self._connect() as conn:
            conn.executemany(
                "UPDATE videos SET auto_pending=? WHERE video_id=?",
                ((int(pending), video_id) for video_id in video_ids),
            )

    def auto_pending_ids(self, channel_id: int) -> set[str]:
        """Read pending stream IDs without making another YouTube request.

        Example: the next scheduled snapshot checks whether a stream ended.
        """

        with self._connect() as conn:
            rows = conn.execute(
                "SELECT video_id FROM videos WHERE subscription_id=? AND auto_pending=1",
                (channel_id,),
            ).fetchall()
        return {str(row[0]) for row in rows}

    def _insert_pipeline_jobs(
        self,
        conn: sqlite3.Connection,
        video_ids: Iterable[str],
        kind: types.PipelineKind,
        *,
        force_live_check: bool = False,
        automatic: bool = False,
    ) -> list[str]:
        """Enqueue in the caller's transaction, e.g. promote a released stream."""

        now = int(time.time())
        requested_ns = time.time_ns()
        queued = []
        for video_id in video_ids:
            cursor = conn.execute(
                """
                INSERT INTO pipeline_jobs(
                    video_id, kind, state, stage, label, started_at, updated_at,
                    requested_ns, force_live_check
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(video_id) DO NOTHING
                """,
                (video_id, kind, types.PipelineJobState.QUEUED, progress.Stage.QUEUED,
                 progress.stage_label(progress.Stage.QUEUED), now, now, requested_ns, force_live_check),
            )
            if cursor.rowcount:
                queued.append(video_id)
            if automatic:
                conn.execute("UPDATE videos SET auto_pending=0 WHERE video_id=?", (video_id,))
        return queued

    def claim_pipeline_job(self, job: types.PipelineJob) -> types.PipelineJob | None:
        """Acquire the exact ready request once, e.g. skip work cancelled after dispatch."""

        now = int(time.time())
        with self._connect() as conn:
            row = conn.execute(
                """
                UPDATE pipeline_jobs
                SET state=?, updated_at=?, owner_pid=?, owner_started_at=?, retry_at=NULL
                WHERE video_id=? AND kind=? AND requested_ns IS ? AND state IN (?, ?)
                  AND (retry_at IS NULL OR retry_at<=?)
                RETURNING *
                """,
                (
                    types.PipelineJobState.RUNNING,
                    now,
                    os.getpid(),
                    psutil.Process().create_time(),
                    job.video_id,
                    job.kind,
                    job.requested_ns,
                    *_WAITING,
                    now,
                ),
            ).fetchone()
            return self._pipeline_job(row) if row else None

    def update_pipeline_job(self, update: progress.Update) -> None:
        """Checkpoint a reached stage and overall fraction for crash recovery.

        Example: completing download records the start of audio extraction.
        """

        with self._connect() as conn:
            conn.execute(
                """
                UPDATE pipeline_jobs
                SET stage=?, fraction=?, label=?, updated_at=?
                WHERE video_id=?
                """,
                (
                    update.stage.value,
                    progress.overall_fraction(update),
                    update.label,
                    int(time.time()),
                    update.video_id,
                ),
            )

    def set_pipeline_job_paused(self, video_id: str, paused: bool) -> None:
        """Persist whether the live process tree is deliberately suspended.

        Example: Pause writes `paused`; Resume returns the job to `running`.
        """

        state = types.PipelineJobState.PAUSED if paused else types.PipelineJobState.RUNNING
        with self._connect() as conn:
            conn.execute(
                "UPDATE pipeline_jobs SET state=?, updated_at=? WHERE video_id=?",
                (state.value, int(time.time()), video_id),
            )

    def finish_pipeline_job(self, video_id: str) -> None:
        """Retire the request after success or explicit cancellation.

        Example: cancelling a manual stream download also clears auto-pending.
        """

        with self._connect() as conn:
            conn.execute("DELETE FROM pipeline_jobs WHERE video_id=?", (video_id,))
            conn.execute("UPDATE videos SET auto_pending=0 WHERE video_id=?", (video_id,))

    def interrupt_pipeline_job(self, video_id: str, *, failed: bool = False) -> None:
        """Release ownership and keep the checkpoint, e.g. defer a network failure."""

        now = int(time.time())
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE pipeline_jobs SET state=?, owner_pid=NULL, owner_started_at=NULL, updated_at=?,
                    retry_at=CASE WHEN ? THEN ? + MIN(3600, 60 * (1 << MIN(failures, 6))) END,
                    failures=failures + ?
                WHERE video_id=?
                """,
                (types.PipelineJobState.INTERRUPTED, now, failed, now, failed, video_id),
            )

    def retry_pipeline_now(self, video_id: str, *, force_live_check: bool | None = None) -> None:
        """Release the cooldown for an explicit retry, e.g. after reconnecting."""

        with self._connect() as conn:
            conn.execute(
                """
                UPDATE pipeline_jobs SET retry_at=NULL, force_live_check=COALESCE(?, force_live_check)
                WHERE video_id=? AND state IN (?, ?)
                """,
                (force_live_check, video_id, *_WAITING),
            )

    def cancel_waiting_pipeline(self, video_id: str) -> bool:
        """Discard a waiting request without stopping the active worker."""

        with self._connect() as conn:
            removed = conn.execute("DELETE FROM pipeline_jobs WHERE video_id=? AND state IN (?, ?)",
                                   (video_id, *_WAITING)).rowcount
            if removed:
                conn.execute("UPDATE videos SET auto_pending=0 WHERE video_id=?", (video_id,))
            return bool(removed)

    def recover_pipeline_jobs(self) -> list[types.PipelineJob]:
        """Mark stale live states interrupted and return them in start order.

        Example: GUI startup offers to resume the job active during a crash.
        """

        with self._connect() as conn:
            live = conn.execute(
                "SELECT * FROM pipeline_jobs WHERE state IN (?, ?)",
                (types.PipelineJobState.RUNNING.value, types.PipelineJobState.PAUSED.value),
            ).fetchall()
            for row in live:
                if not _owner_is_alive(row):
                    conn.execute(
                        """
                        UPDATE pipeline_jobs SET state=?, owner_pid=NULL, owner_started_at=NULL, retry_at=NULL
                        WHERE video_id=? AND state IN (?, ?)
                          AND owner_pid IS ? AND owner_started_at IS ?
                        """,
                        (types.PipelineJobState.INTERRUPTED, row["video_id"],
                         types.PipelineJobState.RUNNING, types.PipelineJobState.PAUSED,
                         row["owner_pid"], row["owner_started_at"]),
                    )
            rows = conn.execute(
                "SELECT * FROM pipeline_jobs WHERE state=? ORDER BY rowid",
                (types.PipelineJobState.INTERRUPTED.value,),
            ).fetchall()
        return [self._pipeline_job(row) for row in rows]

    def pipeline_job(self, video_id: str) -> types.PipelineJob | None:
        """Read one durable recovery marker for contextual GUI actions.

        Example: an interrupted row changes Download into Resume.
        """

        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pipeline_jobs WHERE video_id=?",
                (video_id,),
            ).fetchone()
        return self._pipeline_job(row) if row else None

    def pipeline_jobs(self) -> list[types.PipelineJob]:
        """Read execution and waiting state in request order, e.g. for the queue UI."""

        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM pipeline_jobs ORDER BY rowid").fetchall()
        return [self._pipeline_job(row) for row in rows]

    @staticmethod
    def _pipeline_job(row: sqlite3.Row) -> types.PipelineJob:
        """Decode one SQLite recovery row into its typed domain value.

        Example: `_pipeline_job(row).kind` is a `PipelineKind`.
        """

        return types.PipelineJob(
            str(row["video_id"]),
            types.PipelineKind(str(row["kind"])),
            types.PipelineJobState(str(row["state"])),
            str(row["stage"]),
            float(row["fraction"]),
            str(row["label"]),
            int(row["started_at"]),
            int(row["updated_at"]),
            row["requested_ns"],
            bool(row["force_live_check"]),
            row["retry_at"],
        )
