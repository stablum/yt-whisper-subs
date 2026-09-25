"""Apply bounded channel snapshots and select safe automatic downloads.

Example: `LibraryService` mixes in `ChannelServiceMixin` for scheduled checks.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from datetime import timezone

from yt_whisper_subs import cfg
from yt_whisper_subs import library_feed
from yt_whisper_subs import library_types as types


ReportFn = Callable[[str], None]


class ChannelServiceMixin:
    """Keep channel discovery and live-transition policy together.

    Example: `_check_channel` releases a pending stream after a later listing.
    """

    def _check_channel(
        self,
        channel: types.Channel,
        report: ReportFn,
        sidecars: dict[str, types.VideoMeta],
    ) -> list[str]:
        """Persist one snapshot and select safe future auto-download candidates.

        Example: `_check_channel(channel, report, sidecars)` returns new IDs after baseline.
        """

        initial_check = channel.baseline_at is None
        known_ids = self.db.channel_video_ids(channel.channel_id)
        policy = self._channel_policy()
        snapshot = self._feed.channel(channel.url, policy, known_ids)
        snapshot = self._apply_sidecars(snapshot, sidecars, policy.published_after)
        result = self.db.store_snapshot(channel.channel_id, snapshot)
        new_ids = [video_id for video_id in result.new_ids if video_id not in sidecars]
        if not snapshot.complete:
            warning = "Partial refresh: Streams could not be checked; history preserved"
            self.db.set_channel_error(channel.channel_id, warning)
            report(f"{snapshot.title}: {warning}")
        entry_word = "entry" if result.pruned == 1 else "entries"
        pruned = f", {result.pruned} old remote {entry_word} removed"
        report(
            f"{snapshot.title}: {len(snapshot.videos)} retained, "
            f"{len(new_ids)} new{pruned if result.pruned else ''}"
        )
        if initial_check or not channel.auto_download:
            return []
        statuses = {
            meta.identity.video_id: meta.details.live_status
            for meta in snapshot.videos
        }
        pending_new = {video_id for video_id in new_ids if statuses[video_id] not in types.LIVE_READY}
        if pending_new:
            self.db.set_auto_pending(pending_new, True)
        ready_new = [video_id for video_id in new_ids if video_id not in pending_new]
        pending_ids = self.db.auto_pending_ids(channel.channel_id)
        ready_pending = [
            meta.identity.video_id
            for meta in snapshot.videos
            if meta.identity.video_id in pending_ids
            and meta.details.live_status in types.LIVE_READY
        ]
        return [*ready_new, *ready_pending]

    @staticmethod
    def _apply_sidecars(
        snapshot: types.ChannelSnapshot,
        sidecars: dict[str, types.VideoMeta],
        published_after: int | None,
    ) -> types.ChannelSnapshot:
        """Use durable metadata before deciding if flat channel rows are retained.

        Example: an undated old row stays excluded after its first full lookup.
        """

        videos = []
        for meta in snapshot.videos:
            cached = sidecars.get(meta.identity.video_id)
            if cached is None:
                videos.append(meta)
                continue
            fresh = meta.details
            old = cached.details
            title = meta.identity.title
            ident = meta.identity._replace(
                title=title if title != meta.identity.video_id else cached.identity.title,
            )
            origin = meta.origin._replace(
                channel=meta.origin.channel if meta.origin.channel != "Unknown channel" else cached.origin.channel,
                channel_id=meta.origin.channel_id or cached.origin.channel_id,
                published_at=(
                    meta.origin.published_at
                    if meta.origin.published_at is not None
                    else cached.origin.published_at
                ),
            )
            status = fresh.live_status
            if status is None and old.live_status in types.LIVE_BLOCKED:
                status = types.LIVE_UNKNOWN
            details = fresh._replace(
                duration=fresh.duration if fresh.duration is not None else old.duration,
                view_count=fresh.view_count if fresh.view_count is not None else old.view_count,
                description=fresh.description or old.description,
                thumbnail_url=fresh.thumbnail_url or old.thumbnail_url,
                live_status=status,
            )
            videos.append(types.VideoMeta(ident, origin, details))
        if published_after is not None:
            videos = [
                meta
                for meta in videos
                if meta.origin.published_at is None
                or meta.origin.published_at >= published_after
            ]
        return snapshot._replace(videos=videos)

    def _channel_policy(self) -> library_feed.ChannelScanPolicy:
        """Build bounded discovery policy from validated persisted settings.

        Example: the default scans 50 recent entries from each channel tab.
        """

        raw_limit = self.db.setting(
            "channel_recent_limit",
            str(cfg.DEFAULT_LIBRARY_CHANNEL_RECENT_LIMIT),
        )
        try:
            requested_limit = int(raw_limit)
        except ValueError:
            requested_limit = cfg.DEFAULT_LIBRARY_CHANNEL_RECENT_LIMIT
        limit = min(
            cfg.MAX_LIBRARY_CHANNEL_RECENT_LIMIT,
            max(1, requested_limit),
        )
        published_after = None
        cutoff = self.db.setting("channel_published_after", "").strip()
        try:
            if cutoff:
                value = datetime.strptime(cutoff, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                published_after = int(value.timestamp())
        except ValueError:
            pass
        return library_feed.ChannelScanPolicy(
            limit,
            cfg.MAX_LIBRARY_CHANNEL_RECENT_LIMIT,
            published_after,
        )
