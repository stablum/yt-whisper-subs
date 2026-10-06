"""Persist crash-recoverable pipeline jobs as one focused database concern.

Example: `LibraryDb` composes `PipelineJobDbMixin` beside its catalog methods.
"""

from __future__ import annotations

import os
import sqlite3
import time

import psutil

from yt_whisper_subs import library_types as types
from yt_whisper_subs import pipeline_progress as progress


SCHEMA = """
CREATE TABLE IF NOT EXISTS pipeline_jobs (
    video_id TEXT PRIMARY KEY REFERENCES videos(video_id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    stage TEXT NOT NULL,
    fraction REAL NOT NULL DEFAULT 0,
    label TEXT NOT NULL,
    started_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    owner_pid INTEGER,
    owner_started_at REAL
);
"""


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

    def begin_pipeline_job(self, video_id: str, kind: types.PipelineKind) -> None:
        """Persist a running pipeline before launching its child process.

        Example: a power loss after this write becomes recoverable at next start.
        """

        now = int(time.time())
        with self._connect() as conn:
            # Keep older interrupted jobs while the serial lane starts new work.
            conn.execute(
                """
                INSERT INTO pipeline_jobs(
                    video_id, kind, state, stage, fraction, label, started_at, updated_at,
                    owner_pid, owner_started_at
                ) VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
                ON CONFLICT(video_id) DO UPDATE SET
                    kind=excluded.kind,
                    state=excluded.state,
                    stage=excluded.stage,
                    fraction=excluded.fraction,
                    label=excluded.label,
                    started_at=excluded.started_at,
                    updated_at=excluded.updated_at,
                    owner_pid=excluded.owner_pid,
                    owner_started_at=excluded.owner_started_at
                """,
                (
                    video_id,
                    kind.value,
                    types.PipelineJobState.RUNNING.value,
                    progress.Stage.QUEUED.value,
                    progress.stage_label(progress.Stage.QUEUED),
                    now,
                    now,
                    os.getpid(),
                    psutil.Process().create_time(),
                ),
            )

    def update_pipeline_job(self, update: progress.Update) -> None:
        """Checkpoint a reached stage and overall fraction for crash recovery.

        Example: completing download records the start of audio extraction.
        """

        with self._connect() as conn:
            conn.execute(
                """
                UPDATE pipeline_jobs
                SET state=?, stage=?, fraction=?, label=?, updated_at=?
                WHERE video_id=?
                """,
                (
                    types.PipelineJobState.RUNNING.value,
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
        """Remove a normally completed, failed, or user-cancelled recovery marker.

        Example: only an unclean process exit intentionally leaves a job behind.
        """

        with self._connect() as conn:
            conn.execute("DELETE FROM pipeline_jobs WHERE video_id=?", (video_id,))

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
                        UPDATE pipeline_jobs SET state=? WHERE video_id=?
                          AND owner_pid IS ? AND owner_started_at IS ?
                        """,
                        (types.PipelineJobState.INTERRUPTED.value, row["video_id"],
                         row["owner_pid"], row["owner_started_at"]),
                    )
            rows = conn.execute(
                "SELECT * FROM pipeline_jobs WHERE state=? ORDER BY started_at, video_id",
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

    def interrupted_pipeline_jobs(self) -> list[types.PipelineJob]:
        """Read resumable markers without changing any running job state.

        Example: a table refresh restores only genuinely interrupted labels.
        """

        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM pipeline_jobs WHERE state=? ORDER BY started_at, video_id",
                (types.PipelineJobState.INTERRUPTED.value,),
            ).fetchall()
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
        )
