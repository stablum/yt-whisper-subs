"""Reconcile local media and sidecar metadata with catalog rows.

Example: `LibraryDb` mixes in `MediaDbMixin` for one batched disk scan.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterable

from yt_whisper_subs import library_types as types


class MediaDbMixin:
    """Own media-table writes without coupling channel discovery to file scans.

    Example: `db.reconcile_media(scanned)` replaces stale local paths.
    """

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

    def refresh_known_metadata(self, metas: Iterable[types.VideoMeta]) -> int:
        """Apply durable sidecars only to rows already admitted to the catalog.

        Example: `db.refresh_known_metadata(sidecars.values())` clears stale queue work.
        """

        checked_at = int(time.time())
        updated = 0
        with self._connect() as conn:
            known_ids = {str(row[0]) for row in conn.execute("SELECT video_id FROM videos")}
            for meta in metas:
                if meta.identity.video_id not in known_ids:
                    continue
                self._upsert_video(
                    conn,
                    meta,
                    None,
                    checked_at,
                    metadata_checked_at=checked_at,
                )
                updated += 1
        return updated

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
