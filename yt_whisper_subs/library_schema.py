"""Create and evolve the catalog schema independently from video operations.

Example: `LibraryDb` composes `SchemaDbMixin` during startup.
"""

from __future__ import annotations

import json

from yt_whisper_subs import library_job_db
from yt_whisper_subs import vocabulary_db
from yt_whisper_subs import practice_db
from yt_whisper_subs import verb_schema
from yt_whisper_subs import sentence_schema
from yt_whisper_subs import sentence_queue_db
from yt_whisper_subs import practice_ready
from yt_whisper_subs import practice_stock
from yt_whisper_subs import vocabulary_learning_db
from yt_whisper_subs import word_reviews
from yt_whisper_subs import listening_db
from yt_whisper_subs import vocabulary_accretion_schema
from yt_whisper_subs import vocabulary_cleanup
from yt_whisper_subs import comprehension_schema
from yt_whisper_subs import word_order_db


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

{vocabulary_db.SCHEMA}

{practice_db.SCHEMA}

{verb_schema.SCHEMA}

{sentence_schema.SCHEMA}
{sentence_queue_db.SCHEMA}
{listening_db.SCHEMA}
{practice_ready.SCHEMA}
{practice_stock.SCHEMA}
{vocabulary_learning_db.SCHEMA}
{word_reviews.SCHEMA}
{vocabulary_accretion_schema.SCHEMA}
{vocabulary_cleanup.SCHEMA}
{comprehension_schema.SCHEMA}
{word_order_db.SCHEMA}

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_videos_subscription ON videos(subscription_id);
CREATE INDEX IF NOT EXISTS idx_videos_published ON videos(published_at DESC);
"""


def _upgrade_skills(conn) -> None:
    """Widen skill constraints atomically, preserving IDs and references; e.g. existing queued translations survive the listening upgrade."""

    tables = []
    for family, skill in (("sentence", "listen_nl"), ("practice", "sound_nl")):
        for suffix in ("questions", "sessions", "states"):
            name = f"{family}_{suffix}"
            row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
            if row and f"'{skill}'" not in row[0]:
                tables.append((name, row[0], skill))
    if not tables:
        return
    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        for name, sql, skill in tables:
            indices = [row[0] for row in conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (name,))]
            widened = sql.replace("'nl_en','en_nl'", f"'nl_en','en_nl','{skill}'")
            if widened == sql:
                raise RuntimeError(f"Unknown practice skill constraint in {name}")
            # Create-copy-drop-rename keeps every child FK pointed at the original table name.
            conn.execute(widened.replace(name, name + "_upgrade", 1))
            conn.execute(f"INSERT INTO {name}_upgrade SELECT * FROM {name}")
            conn.execute(f"DROP TABLE {name}")
            conn.execute(f"ALTER TABLE {name}_upgrade RENAME TO {name}")
            for index in indices:
                conn.execute(index)
        if any(name == "practice_sessions" for name, _, _ in tables):
            # A saved full-practice position refers to the order in which that run began.
            order = ["practice:nl_en", "practice:en_nl", "verb", "sentence:en_nl", "sentence:nl_en",
                     "sentence:listen_nl", "comprehension:read", "comprehension:listen"]
            conn.execute("UPDATE settings SET value=json_set(value,'$.order',json(?)) WHERE key='practice_sequence' "
                         "AND json_valid(value) AND json_type(value)='object' AND json_type(value,'$.order') IS NULL",
                         (json.dumps(order),))
        if conn.execute("PRAGMA foreign_key_check").fetchone():
            raise RuntimeError("Practice skill migration would break catalog references")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


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
            _upgrade_skills(conn)
            conn.executescript(_SCHEMA)
            audit_columns = {row["name"] for row in conn.execute("PRAGMA table_info(vocab_audits)")}
            if "repair_json" not in audit_columns:
                conn.execute("ALTER TABLE vocab_audits ADD COLUMN repair_json TEXT")
            listening_db.upgrade(conn)
            self._practice_upgrade(conn)
            job_columns = {
                str(row["name"]) for row in conn.execute("PRAGMA table_info(pipeline_jobs)")
            }
            for name, sql_type in (("owner_pid", "INTEGER"), ("owner_started_at", "REAL")):
                if name not in job_columns:
                    conn.execute(f"ALTER TABLE pipeline_jobs ADD COLUMN {name} {sql_type}")
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
            self._word_sync(conn)
            self._practice_check_rounds(conn)
