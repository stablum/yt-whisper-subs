"""Persist crash-recoverable pipeline jobs as one focused database concern.

Example: `LibraryDb` composes `PipelineJobDbMixin` beside its catalog methods.
"""

from __future__ import annotations

import sqlite3
import time

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
    updated_at INTEGER NOT NULL
);
"""


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
            # The shared heavy-work lane guarantees only one recoverable pipeline.
            conn.execute("DELETE FROM pipeline_jobs WHERE video_id<>?", (video_id,))
            conn.execute(
                """
                INSERT INTO pipeline_jobs(
                    video_id, kind, state, stage, fraction, label, started_at, updated_at
                ) VALUES (?, ?, ?, ?, 0, ?, ?, ?)
                ON CONFLICT(video_id) DO UPDATE SET
                    kind=excluded.kind,
                    state=excluded.state,
                    stage=excluded.stage,
                    fraction=excluded.fraction,
                    label=excluded.label,
                    started_at=excluded.started_at,
                    updated_at=excluded.updated_at
                """,
                (
                    video_id,
                    kind.value,
                    types.PipelineJobState.RUNNING.value,
                    progress.Stage.QUEUED.value,
                    progress.stage_label(progress.Stage.QUEUED),
                    now,
                    now,
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
            conn.execute(
                """
                UPDATE pipeline_jobs SET state=?
                WHERE state IN (?, ?)
                """,
                (
                    types.PipelineJobState.INTERRUPTED.value,
                    types.PipelineJobState.RUNNING.value,
                    types.PipelineJobState.PAUSED.value,
                ),
            )
            rows = conn.execute(
                "SELECT * FROM pipeline_jobs ORDER BY started_at, video_id"
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
