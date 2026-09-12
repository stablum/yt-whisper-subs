"""Thread-safe SQLite persistence for channel subscriptions and video state.

Example: `LibraryDb(path).initialize()` creates the durable catalog.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import NamedTuple

from yt_whisper_subs import library_types as types
from yt_whisper_subs import library_job_db
from yt_whisper_subs import playback_progress as playback


_SCHEMA = f"""
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL UNIQUE,
    youtube_id TEXT,
    title TEXT NOT NULL,
    auto_download INTEGER NOT NULL DEFAULT 0,
    added_at INTEGER NOT NULL,
    checked_at INTEGER,
    baseline_at INTEGER,
    last_error TEXT,
    pinned_at INTEGER
);

CREATE TABLE IF NOT EXISTS videos (
    video_id TEXT PRIMARY KEY,
    subscription_id INTEGER REFERENCES channels(id) ON DELETE SET NULL,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    channel_title TEXT NOT NULL,
    channel_youtube_id TEXT,
    published_at INTEGER,
    duration REAL,
    view_count INTEGER,
    description TEXT NOT NULL DEFAULT '',
    thumbnail_url TEXT,
    live_status TEXT,
    discovered_at INTEGER NOT NULL,
    metadata_checked_at INTEGER,
    metadata_attempted_at INTEGER,
    download_error TEXT
);

CREATE TABLE IF NOT EXISTS media (
    video_id TEXT PRIMARY KEY REFERENCES videos(video_id) ON DELETE CASCADE,
    path TEXT NOT NULL UNIQUE,
    downloaded_at INTEGER NOT NULL,
    size_bytes INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS playback (
    video_id TEXT PRIMARY KEY REFERENCES videos(video_id) ON DELETE CASCADE,
    position_seconds REAL NOT NULL DEFAULT 0,
    duration_seconds REAL,
    completed_at INTEGER,
    updated_at INTEGER NOT NULL
);

{library_job_db.SCHEMA}

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_videos_subscription ON videos(subscription_id);
CREATE INDEX IF NOT EXISTS idx_videos_published ON videos(published_at DESC);
"""

_VIDEO_SELECT = """
    SELECT
        v.*,
        m.path,
        m.downloaded_at,
        m.size_bytes,
        p.position_seconds AS playback_position_seconds,
        p.duration_seconds AS playback_duration_seconds,
        p.completed_at AS playback_completed_at,
        p.updated_at AS playback_updated_at
    FROM videos v
    LEFT JOIN media m USING(video_id)
    LEFT JOIN playback p USING(video_id)
"""


class SnapshotResult(NamedTuple):
    """Report new discoveries and remote rows removed by bounded retention.

    Example: `result.pruned` feeds a concise channel-check trace message.
    """

    new_ids: list[str]
    pruned: int


class LibraryDb(library_job_db.PipelineJobDbMixin):
    """Own catalog SQL while opening one SQLite connection per worker thread.

    Example: `db.videos(channel_id=1)` lists one subscription's catalog.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self) -> None:
        """Create the catalog and indexes idempotently.

        Example: `db.initialize()` during native app startup.
        """

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(_SCHEMA)
            channel_rows = conn.execute("PRAGMA table_info(channels)").fetchall()
            channel_columns = {str(row["name"]) for row in channel_rows}
            if "pinned_at" not in channel_columns:
                conn.execute("ALTER TABLE channels ADD COLUMN pinned_at INTEGER")
            columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(videos)").fetchall()
            }
            if "metadata_attempted_at" not in columns:
                conn.execute("ALTER TABLE videos ADD COLUMN metadata_attempted_at INTEGER")
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_videos_metadata_queue
                ON videos(metadata_checked_at, metadata_attempted_at, discovered_at DESC)
                """
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a short-lived WAL connection safe for background tasks.

        Example: `with self._connect() as conn: ...`.
        """

        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def add_channel(self, url: str, title: str, auto_download: bool) -> types.Channel:
        """Add a subscription placeholder before its first network check.

        Example: `db.add_channel(url, "@handle", False)` is visible immediately.
        """

        now = int(time.time())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO channels(url, title, auto_download, added_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET auto_download=excluded.auto_download
                """,
                (url, title, int(auto_download), now),
            )
            row = conn.execute("SELECT * FROM channels WHERE url=?", (url,)).fetchone()
        assert row is not None
        return self._channel(row)

    def channels(self) -> list[types.Channel]:
        """List pinned subscriptions first, then alphabetically by shelf.

        Example: `for channel in db.channels(): ...`.
        """

        with self._connect() as conn:
            sql = "SELECT * FROM channels ORDER BY pinned_at IS NULL, pinned_at DESC, title COLLATE NOCASE"
            rows = conn.execute(sql).fetchall()
        return [self._channel(row) for row in rows]

    def channel(self, channel_id: int) -> types.Channel | None:
        """Load one subscription for checking or editing.

        Example: `channel = db.channel(1)`.
        """

        with self._connect() as conn:
            row = conn.execute("SELECT * FROM channels WHERE id=?", (channel_id,)).fetchone()
        return self._channel(row) if row else None

    def set_channel_auto_download(self, channel_id: int, enabled: bool) -> None:
        """Change whether future discoveries start the subtitle pipeline.

        Example: `db.set_channel_auto_download(1, True)`.
        """

        with self._connect() as conn:
            conn.execute(
                "UPDATE channels SET auto_download=? WHERE id=?",
                (int(enabled), channel_id),
            )

    def set_channel_pinned(self, channel_id: int, pinned: bool) -> None:
        """Move one subscription into or out of the priority shelf.

        Example: `db.set_channel_pinned(1, True)` makes it immediately visible.
        """

        pinned_at = time.time_ns() if pinned else None
        with self._connect() as conn:
            conn.execute("UPDATE channels SET pinned_at=? WHERE id=?", (pinned_at, channel_id))

    def remove_channel(self, channel_id: int) -> None:
        """Stop tracking a channel while retaining its downloaded videos.

        Example: `db.remove_channel(1)` removes remote-only rows too.
        """

        with self._connect() as conn:
            conn.execute(
                """
                DELETE FROM videos
                WHERE subscription_id=?
                  AND video_id NOT IN (SELECT video_id FROM media)
                """,
                (channel_id,),
            )
            conn.execute("DELETE FROM channels WHERE id=?", (channel_id,))

    def store_snapshot(self, channel_id: int, snapshot: types.ChannelSnapshot) -> SnapshotResult:
        """Persist a bounded snapshot and prune absent remote-only history.

        Example: `db.store_snapshot(channel_id, snapshot).new_ids` drives automation.
        """

        now = int(time.time())
        baseline_at = now if snapshot.complete else None
        with self._connect() as conn:
            known_ids = {row[0] for row in conn.execute("SELECT video_id FROM videos")}
            new_ids = [
                meta.identity.video_id
                for meta in snapshot.videos
                if meta.identity.video_id not in known_ids
            ]
            conn.execute(
                """
                UPDATE channels
                SET url=?, youtube_id=?, title=?, checked_at=?,
                    baseline_at=COALESCE(baseline_at, ?), last_error=NULL
                WHERE id=?
                """,
                (
                    snapshot.url,
                    snapshot.youtube_id,
                    snapshot.title,
                    now,
                    baseline_at,
                    channel_id,
                ),
            )
            for meta in snapshot.videos:
                metadata_checked_at = now if meta.origin.published_at is not None else None
                self._upsert_video(
                    conn,
                    meta,
                    channel_id,
                    now,
                    metadata_checked_at=metadata_checked_at,
                )
            pruned = self._prune_snapshot(conn, channel_id, snapshot) if snapshot.complete else 0
        return SnapshotResult(new_ids, pruned)

    def channel_video_ids(self, channel_id: int) -> set[str]:
        """Return IDs used to stop adaptive history expansion at overlap.

        Example: `db.channel_video_ids(1)` anchors the next recent scan.
        """

        with self._connect() as conn:
            rows = conn.execute(
                "SELECT video_id FROM videos WHERE subscription_id=?",
                (channel_id,),
            ).fetchall()
        return {str(row[0]) for row in rows}

    def prune_remote_before(self, published_at: int | None) -> int:
        """Delete dated remote-only rows older than an optional cutoff.

        Example: `prune_remote_before(april_2026)` never deletes local media.
        """

        if published_at is None:
            return 0
        with self._connect() as conn:
            cursor = conn.execute(
                """
                DELETE FROM videos
                WHERE published_at < ?
                  AND NOT EXISTS (
                      SELECT 1 FROM media WHERE media.video_id=videos.video_id
                  )
                """,
                (published_at,),
            )
        return max(0, cursor.rowcount)

    def set_channel_error(self, channel_id: int, message: str) -> None:
        """Persist a failed check without discarding its previous catalog.

        Example: `db.set_channel_error(1, "network unavailable")`.
        """

        with self._connect() as conn:
            conn.execute(
                "UPDATE channels SET checked_at=?, last_error=? WHERE id=?",
                (int(time.time()), message, channel_id),
            )

    def upsert_video(self, meta: types.VideoMeta, subscription_id: int | None = None) -> None:
        """Merge richer per-video metadata into an existing discovery row.

        Example: `db.upsert_video(feed.video(url))` backfills a local file.
        """

        now = int(time.time())
        with self._connect() as conn:
            self._upsert_video(conn, meta, subscription_id, now, metadata_checked_at=now)

    def add_local_placeholder(self, meta: types.VideoMeta, local: types.LocalMedia) -> None:
        """Register a scanned media file before remote metadata is available.

        Example: `db.add_local_placeholder(meta, local)` during reconciliation.
        """

        with self._connect() as conn:
            self._upsert_video(conn, meta, None, local.downloaded_at, metadata_checked_at=None)
            self._upsert_local(conn, meta.identity.video_id, local)

    def reconcile_media(self, entries: list[types.ScannedMedia]) -> None:
        """Make database download state match the current videos directory.

        Example: `db.reconcile_media(scanned_entries)` clears stale paths.
        """

        with self._connect() as conn:
            conn.execute("DELETE FROM media")
            checked_at = int(time.time())
            for scanned in entries:
                metadata_checked_at = checked_at if scanned.metadata_complete else None
                self._upsert_video(
                    conn,
                    scanned.meta,
                    None,
                    scanned.local.downloaded_at,
                    metadata_checked_at=metadata_checked_at,
                )
                self._upsert_local(conn, scanned.meta.identity.video_id, scanned.local)

    def set_download_error(self, video_id: str, message: str | None) -> None:
        """Record the last pipeline failure for a visible catalog status.

        Example: `db.set_download_error(video_id, None)` clears an error.
        """

        with self._connect() as conn:
            conn.execute("UPDATE videos SET download_error=? WHERE video_id=?", (message, video_id))

    def videos(
        self,
        *,
        channel_id: int | None = None,
    ) -> list[types.VideoRecord]:
        """Query one channel scope while leaving interactive filtering to Qt.

        Example: `db.videos(channel_id=1)` feeds every smart view for a channel.
        """

        clauses: list[str] = []
        params: list[object] = []
        if channel_id is not None:
            clauses.append("v.subscription_id=?")
            params.append(channel_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
            {_VIDEO_SELECT}
            {where}
            ORDER BY COALESCE(v.published_at, v.discovered_at) DESC, v.title COLLATE NOCASE
        """
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._video(row) for row in rows]

    def video(self, video_id: str) -> types.VideoRecord | None:
        """Load one video for download, details, or playback.

        Example: `record = db.video("dQw4w9WgXcQ")`.
        """

        with self._connect() as conn:
            row = conn.execute(
                f"{_VIDEO_SELECT} WHERE v.video_id=?",
                (video_id,),
            ).fetchone()
        return self._video(row) if row else None

    def record_playback(self, update: playback.Update) -> None:
        """Persist furthest progress while making confirmed completion sticky.

        Example: `db.record_playback(update)` stores one paced mpv observation.
        """

        now = int(time.time())
        completed_at = now if update.completed else None
        position = update.position_seconds
        if update.completed and update.duration_seconds is not None:
            position = max(position, update.duration_seconds)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO playback(
                    video_id, position_seconds, duration_seconds,
                    completed_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(video_id) DO UPDATE SET
                    position_seconds=MAX(playback.position_seconds, excluded.position_seconds),
                    duration_seconds=COALESCE(excluded.duration_seconds, playback.duration_seconds),
                    completed_at=COALESCE(playback.completed_at, excluded.completed_at),
                    updated_at=excluded.updated_at
                """,
                (
                    update.video_id,
                    position,
                    update.duration_seconds,
                    completed_at,
                    now,
                ),
            )

    def metadata_backfill_ids(self, limit: int, retry_before: int) -> list[str]:
        """Select a fair, cooldown-aware metadata work queue slice.

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

    def setting(self, key: str, default: str) -> str:
        """Read a persisted application setting with a supplied default.

        Example: `db.setting("check_hours", "4")`.
        """

        with self._connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else default

    def set_setting(self, key: str, value: str | int | float | bool) -> None:
        """Persist one small GUI or scheduler preference.

        Example: `db.set_setting("check_hours", 4)`.
        """

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO settings(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (key, str(value)),
            )

    @staticmethod
    def _prune_snapshot(
        conn: sqlite3.Connection,
        channel_id: int,
        snapshot: types.ChannelSnapshot,
    ) -> int:
        """Retain this complete recent slice plus every downloaded video.

        Example: a 50-item bounded refresh removes older remote-only rows.
        """

        conn.execute(
            "CREATE TEMP TABLE retained_snapshot(video_id TEXT PRIMARY KEY) WITHOUT ROWID"
        )
        conn.executemany(
            "INSERT INTO retained_snapshot(video_id) VALUES (?)",
            ((meta.identity.video_id,) for meta in snapshot.videos),
        )
        cursor = conn.execute(
            """
            DELETE FROM videos
            WHERE subscription_id=?
              AND NOT EXISTS (
                  SELECT 1 FROM media WHERE media.video_id=videos.video_id
              )
              AND video_id NOT IN (SELECT video_id FROM retained_snapshot)
            """,
            (channel_id,),
        )
        return max(0, cursor.rowcount)

    @staticmethod
    def _upsert_video(
        conn: sqlite3.Connection,
        meta: types.VideoMeta,
        subscription_id: int | None,
        discovered_at: int,
        *,
        metadata_checked_at: int | None,
    ) -> None:
        """Merge one metadata model while keeping richer existing values.

        Example: `_upsert_video(conn, meta, channel_id, now, metadata_checked_at=now)`.
        """

        ident = meta.identity
        origin = meta.origin
        details = meta.details
        values = (
            ident.video_id,
            subscription_id,
            ident.url,
            ident.title,
            origin.channel,
            origin.channel_id,
            origin.published_at,
            details.duration,
            details.view_count,
            details.description,
            details.thumbnail_url,
            details.live_status,
            discovered_at,
            metadata_checked_at,
        )
        conn.execute(
            """
            INSERT INTO videos(
                video_id, subscription_id, url, title, channel_title,
                channel_youtube_id, published_at, duration, view_count,
                description, thumbnail_url, live_status, discovered_at,
                metadata_checked_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(video_id) DO UPDATE SET
                subscription_id=COALESCE(excluded.subscription_id, videos.subscription_id),
                url=excluded.url,
                title=CASE WHEN excluded.title != excluded.video_id THEN excluded.title ELSE videos.title END,
                channel_title=CASE
                    WHEN excluded.channel_title != 'Unknown channel' THEN excluded.channel_title
                    ELSE videos.channel_title
                END,
                channel_youtube_id=COALESCE(excluded.channel_youtube_id, videos.channel_youtube_id),
                published_at=COALESCE(excluded.published_at, videos.published_at),
                duration=COALESCE(excluded.duration, videos.duration),
                view_count=COALESCE(excluded.view_count, videos.view_count),
                description=CASE WHEN excluded.description != '' THEN excluded.description ELSE videos.description END,
                thumbnail_url=COALESCE(excluded.thumbnail_url, videos.thumbnail_url),
                live_status=COALESCE(excluded.live_status, videos.live_status),
                metadata_checked_at=COALESCE(excluded.metadata_checked_at, videos.metadata_checked_at)
            """,
            values,
        )

    @staticmethod
    def _upsert_local(conn: sqlite3.Connection, video_id: str, local: types.LocalMedia) -> None:
        """Insert one scanned file after its parent video row exists.

        Example: `_upsert_local(conn, video_id, local)`.
        """

        conn.execute(
            """
            INSERT INTO media(video_id, path, downloaded_at, size_bytes)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(video_id) DO UPDATE SET
                path=excluded.path,
                downloaded_at=excluded.downloaded_at,
                size_bytes=excluded.size_bytes
            """,
            (video_id, str(local.path), local.downloaded_at, local.size_bytes),
        )

    @staticmethod
    def _channel(row: sqlite3.Row) -> types.Channel:
        """Map one SQLite channel row into the shared record.

        Example: `_channel(row).auto_download` is a bool.
        """

        return types.Channel(
            channel_id=int(row["id"]),
            url=str(row["url"]),
            youtube_id=str(row["youtube_id"]) if row["youtube_id"] else None,
            title=str(row["title"]),
            auto_download=bool(row["auto_download"]),
            added_at=int(row["added_at"]),
            checked_at=int(row["checked_at"]) if row["checked_at"] is not None else None,
            baseline_at=int(row["baseline_at"]) if row["baseline_at"] is not None else None,
            last_error=str(row["last_error"]) if row["last_error"] else None,
            pinned_at=int(row["pinned_at"]) if row["pinned_at"] is not None else None,
        )

    @staticmethod
    def _video(row: sqlite3.Row) -> types.VideoRecord:
        """Compose one joined SQLite row into the nested video record.

        Example: `_video(row).meta.identity.title`.
        """

        ident = types.VideoIdentity(str(row["video_id"]), str(row["url"]), str(row["title"]))
        origin = types.VideoOrigin(
            str(row["channel_title"]),
            str(row["channel_youtube_id"]) if row["channel_youtube_id"] else None,
            int(row["published_at"]) if row["published_at"] is not None else None,
        )
        details = types.VideoDetails(
            float(row["duration"]) if row["duration"] is not None else None,
            int(row["view_count"]) if row["view_count"] is not None else None,
            str(row["description"]),
            str(row["thumbnail_url"]) if row["thumbnail_url"] else None,
            str(row["live_status"]) if row["live_status"] else None,
        )
        local = None
        if row["path"]:
            local = types.LocalMedia(Path(row["path"]), int(row["downloaded_at"]), int(row["size_bytes"]))
        playback_state = None
        if row["playback_updated_at"] is not None:
            playback_state = types.PlaybackState(
                float(row["playback_position_seconds"]),
                float(row["playback_duration_seconds"])
                if row["playback_duration_seconds"] is not None
                else None,
                int(row["playback_completed_at"])
                if row["playback_completed_at"] is not None
                else None,
                int(row["playback_updated_at"]),
            )
        return types.VideoRecord(
            types.VideoMeta(ident, origin, details),
            int(row["subscription_id"]) if row["subscription_id"] is not None else None,
            int(row["discovered_at"]),
            local,
            str(row["download_error"]) if row["download_error"] else None,
            playback_state,
        )
