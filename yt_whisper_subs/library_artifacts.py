"""Cache filesystem-derived library state outside Qt's paint hot path.

Example: `ArtifactCache(out_dir).catalog(db.videos())` prepares one UI snapshot.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import NamedTuple

from yt_whisper_subs import chapters
from yt_whisper_subs import library_types as types
from yt_whisper_subs import srt


type FileStamp = tuple[str, int, int]
type PipelineStamp = tuple[FileStamp, FileStamp, FileStamp]


class CatalogState(NamedTuple):
    """Bundle records with precomputed artifact health for one model reset.

    Example: `state.issues[video_id]` is safe to read while painting.
    """

    records: list[types.VideoRecord]
    issues: dict[str, str | None]


class _IssueEntry(NamedTuple):
    """Retain one validation result until its exact input files change.

    Example: `_IssueEntry(stamp, None)` caches a healthy pipeline.
    """

    stamp: PipelineStamp
    issue: str | None


class _ChapterEntry(NamedTuple):
    """Retain one parsed chapter plan until its JSON archive changes.

    Example: `_ChapterEntry(stamp, chapter_set)` avoids repeat JSON reads.
    """

    stamp: FileStamp
    chapter_set: chapters.ChapterSet | None


def _stamp(path: Path) -> FileStamp:
    """Describe the path, size, and nanosecond modification time cheaply.

    Example: `_stamp(sidecar)` changes whenever a subtitle is replaced.
    """

    try:
        stat = path.stat()
    except OSError:
        return str(path), -1, -1
    return str(path), stat.st_size, stat.st_mtime_ns


def pipeline_issue(record: types.VideoRecord) -> str | None:
    """Describe a missing or corrupt required subtitle beside local media.

    Example: a NUL-only Dutch SRT returns `Dutch subtitles are invalid`.
    """

    if not record.local:
        return None
    video = record.local.path
    if not video.is_file():
        return None
    primary = video.with_suffix(".srt")
    english = video.with_name(f"{video.stem}.en.srt")
    if not srt.file_has_cues(primary):
        return "Dutch subtitles are missing or invalid"
    if not srt.file_has_cues(english):
        return "English subtitles are missing or invalid"
    return None


class ArtifactCache:
    """Memoize parsed sidecars while detecting edits through file stamps.

    Example: `cache.issue(record)` validates unchanged SRT files only once.
    """

    def __init__(self, out_dir: Path) -> None:
        self._out_dir = out_dir
        self._issues: dict[str, _IssueEntry] = {}
        self._chapters: dict[str, _ChapterEntry] = {}
        self._lock = threading.RLock()

    def catalog(self, records: list[types.VideoRecord]) -> CatalogState:
        """Prepare artifact health once for an entire durable catalog load.

        Example: the GUI calls `catalog(records)` only when catalog data changes.
        """

        issues = {
            record.meta.identity.video_id: self.issue(record)
            for record in records
        }
        video_ids = set(issues)
        with self._lock:
            self._issues = {
                video_id: entry
                for video_id, entry in self._issues.items()
                if video_id in video_ids
            }
            self._chapters = {
                video_id: entry
                for video_id, entry in self._chapters.items()
                if video_id in video_ids
            }
        return CatalogState(records, issues)

    def issue(self, record: types.VideoRecord) -> str | None:
        """Return cached subtitle health, revalidating only changed files.

        Example: `cache.issue(record)` is constant-time after the first read.
        """

        if not record.local:
            return None
        video = record.local.path
        primary = video.with_suffix(".srt")
        english = video.with_name(f"{video.stem}.en.srt")
        stamp = (_stamp(video), _stamp(primary), _stamp(english))
        video_id = record.meta.identity.video_id
        with self._lock:
            cached = self._issues.get(video_id)
            if cached and cached.stamp == stamp:
                return cached.issue
        issue = pipeline_issue(record)
        with self._lock:
            self._issues[video_id] = _IssueEntry(stamp, issue)
        return issue

    def chapter_set(self, video_id: str) -> chapters.ChapterSet | None:
        """Return a cached parsed chapter archive for the selected video.

        Example: repeated row clicks reuse `chapter_set("abc")`.
        """

        files = chapters.ChapterFiles.for_video(self._out_dir, video_id)
        stamp = _stamp(files.archive)
        with self._lock:
            cached = self._chapters.get(video_id)
            if cached and cached.stamp == stamp:
                return cached.chapter_set
        chapter_set = files.load()
        with self._lock:
            self._chapters[video_id] = _ChapterEntry(stamp, chapter_set)
        return chapter_set
