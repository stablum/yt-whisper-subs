"""Create and evolve the catalog schema independently from video operations.

Example: `LibraryDb` composes `SchemaDbMixin` during startup.
"""

from __future__ import annotations

from yt_whisper_subs import library_job_db


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
    live_retry_at INTEGER,
    auto_pending INTEGER NOT NULL DEFAULT 0,
    discovered_at INTEGER NOT NULL,
    metadata_checked_at INTEGER,
    metadata_attempted_at INTEGER,
    download_error TEXT,
    download_error_sig TEXT
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

CREATE TABLE IF NOT EXISTS video_history (
    video_id TEXT PRIMARY KEY REFERENCES videos(video_id) ON DELETE CASCADE,
    downloaded_at INTEGER,
    removed_at INTEGER,
    files_removed INTEGER NOT NULL DEFAULT 0
);

{library_job_db.SCHEMA}

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_videos_subscription ON videos(subscription_id);
CREATE INDEX IF NOT EXISTS idx_videos_published ON videos(published_at DESC);
"""


class SchemaDbMixin:
    """Own startup schema changes without growing the catalog query module.

    Example: `db.initialize()` creates the tables and required indexes.
    """

    def initialize(self) -> None:
        """Create the catalog and indexes idempotently.

        Example: `db.initialize()` during native app startup.
        """

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(_SCHEMA)
            # Seed existing downloads before a scan can discard their media paths.
            conn.execute(
                """
                INSERT OR IGNORE INTO video_history(video_id, downloaded_at)
                SELECT video_id, downloaded_at FROM media
                """
            )
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
            if "live_retry_at" not in columns:
                conn.execute("ALTER TABLE videos ADD COLUMN live_retry_at INTEGER")
            if "auto_pending" not in columns:
                conn.execute("ALTER TABLE videos ADD COLUMN auto_pending INTEGER NOT NULL DEFAULT 0")
            if "download_error_sig" not in columns:
                conn.execute("ALTER TABLE videos ADD COLUMN download_error_sig TEXT")
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_videos_metadata_queue
                ON videos(metadata_checked_at, metadata_attempted_at, discovered_at DESC)
                """
            )
