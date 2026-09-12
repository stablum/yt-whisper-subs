"""Composable records shared by the library database, feed, and GUI layers.

Example: `VideoMeta(identity, origin, details)` describes one YouTube item.
"""

from __future__ import annotations

from pathlib import Path
from enum import StrEnum
from typing import NamedTuple
from typing import Any


class VideoIdentity(NamedTuple):
    """Identify one video without mixing in origin or local-file state.

    Example: `VideoIdentity("id", url, "A title")`.
    """

    video_id: str
    url: str
    title: str


class VideoOrigin(NamedTuple):
    """Describe where and when a YouTube video was published.

    Example: `VideoOrigin("Channel", "UC...", timestamp)`.
    """

    channel: str
    channel_id: str | None
    published_at: int | None


class VideoDetails(NamedTuple):
    """Hold optional discovery details that improve library browsing.

    Example: `VideoDetails(90, 1000, "...", thumbnail, "not_live")`.
    """

    duration: float | None
    view_count: int | None
    description: str
    thumbnail_url: str | None
    live_status: str | None


class VideoMeta(NamedTuple):
    """Compose remote video facts independently from download state.

    Example: `VideoMeta(identity, origin, details)`.
    """

    identity: VideoIdentity
    origin: VideoOrigin
    details: VideoDetails


class VideoInfo(NamedTuple):
    """Keep stable catalog metadata beside the complete extractor document.

    Example: `VideoInfo(meta, raw_json)` preserves all yt-dlp fields.
    """

    meta: VideoMeta
    document: dict[str, Any]


class LocalMedia(NamedTuple):
    """Describe the durable downloaded file for a catalog video.

    Example: `LocalMedia(path, downloaded_at, size_bytes)`.
    """

    path: Path
    downloaded_at: int
    size_bytes: int


class ScannedMedia(NamedTuple):
    """Pair a local file with metadata-sidecar completeness.

    Example: `ScannedMedia(meta, local, metadata_complete=True)`.
    """

    meta: VideoMeta
    local: LocalMedia
    metadata_complete: bool


class PlaybackState(NamedTuple):
    """Persist furthest playback separately from confirmed completion.

    Example: `PlaybackState(30, 120, None, now)` represents partial viewing.
    """

    position_seconds: float
    duration_seconds: float | None
    completed_at: int | None
    updated_at: int


class PipelineKind(StrEnum):
    """Name a durable operation that can be recovered after interruption.

    Example: `PipelineKind.DOWNLOAD` resumes the complete subtitle pipeline.
    """

    DOWNLOAD = "download"
    CHAPTERS = "chapters"


class PipelineJobState(StrEnum):
    """Distinguish live, paused, and restart-recoverable pipeline work.

    Example: startup converts a stale `RUNNING` job to `INTERRUPTED`.
    """

    RUNNING = "running"
    PAUSED = "paused"
    INTERRUPTED = "interrupted"


class PipelineJob(NamedTuple):
    """Persist enough pipeline state to explain and resume interrupted work.

    Example: `job.fraction` restores the reached graphical progress position.
    """

    video_id: str
    kind: PipelineKind
    state: PipelineJobState
    stage: str
    fraction: float
    label: str
    started_at: int
    updated_at: int


class VideoRecord(NamedTuple):
    """Combine remote metadata, subscription ownership, and local media.

    Example: `record.local is not None` means the video is downloaded.
    """

    meta: VideoMeta
    subscription_id: int | None
    discovered_at: int
    local: LocalMedia | None
    download_error: str | None
    playback: PlaybackState | None

    @property
    def downloaded(self) -> bool:
        """Expose local availability for table filters and status labels.

        Example: `if record.downloaded: play(record)`.
        """

        return self.local is not None


class Channel(NamedTuple):
    """Represent one persisted YouTube channel subscription.

    Example: `channel.auto_download` controls future-video downloads.
    """

    channel_id: int
    url: str
    youtube_id: str | None
    title: str
    auto_download: bool
    added_at: int
    checked_at: int | None
    baseline_at: int | None
    last_error: str | None
    pinned_at: int | None

    @property
    def pinned(self) -> bool:
        """Expose whether this channel belongs to the priority shelf.

        Example: `channel.pinned` chooses the starred sidebar section.
        """

        return self.pinned_at is not None


class ChannelSnapshot(NamedTuple):
    """Return a channel slice and whether all tabs were safe to synchronize.

    Example: `snapshot.complete` permits pruning entries absent from the slice.
    """

    url: str
    youtube_id: str | None
    title: str
    videos: list[VideoMeta]
    complete: bool
