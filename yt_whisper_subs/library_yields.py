"""Exact, non-recursive inventory and removal of library videos' yields.

Example: `VideoYields.inspect(root, video_id).remove(report)` deletes one manifest.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import NamedTuple


_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
_FOLDERS = ("videos", "audio", "metadata", "subtitles", "chapters", "logs")


class VideoYields(NamedTuple):
    """Hold an already validated, exact per-video deletion manifest.

    Example: the confirmation dialog displays `manifest.paths` before removal.
    """

    root: Path
    video_id: str
    paths: tuple[Path, ...]

    @classmethod
    def inspect(cls, root: Path, video_id: str) -> VideoYields:
        """Enumerate only exact-ID files immediately inside managed folders.

        Example: ID `abcdefghijk` cannot match another video's prefix.
        """

        return cls.inspect_many(root, (video_id,))[0]

    @classmethod
    def inspect_many(cls, root: Path, video_ids: Iterable[str]) -> tuple[VideoYields, ...]:
        """Scan each managed folder once for an explicit set of catalog IDs.

        Example: a filtered cleanup inventories 500 videos in one directory pass.
        """

        paths_by_id: dict[str, list[Path]] = {video_id: [] for video_id in video_ids}
        for video_id in paths_by_id:
            if not _VIDEO_ID.fullmatch(video_id):
                raise ValueError(f"invalid YouTube video ID: {video_id!r}")
        resolved_root = root.resolve()
        for folder in _FOLDERS:
            directory = resolved_root / folder
            if directory.resolve() != directory:
                raise RuntimeError(f"refusing redirected managed folder: {directory}")
            if not directory.is_dir():
                continue
            for path in directory.iterdir():
                video_id = path.name[:11]
                if video_id not in paths_by_id or not cls._matches(folder, path.name, video_id):
                    continue
                if not (path.is_file() or path.is_symlink()):
                    continue
                paths_by_id[video_id].append(path)
        return tuple(
            cls(resolved_root, video_id, tuple(sorted(paths)))
            for video_id, paths in paths_by_id.items()
        )

    @staticmethod
    def _matches(folder: str, name: str, video_id: str) -> bool:
        """Require an ID delimiter and expected log suffix before inclusion.

        Example: `abcdefghijk2.srt` never matches ID `abcdefghijk`.
        """

        if folder == "logs":
            return name.startswith(f"{video_id}-") and name.casefold().endswith(".log")
        return name.startswith(f"{video_id}.")

    def validate(self) -> None:
        """Reject forged manifests and redirected folders before any unlink.

        Example: a path to another video's subtitle cannot be approved for this ID.
        """

        if not _VIDEO_ID.fullmatch(self.video_id) or self.root != self.root.resolve():
            raise RuntimeError("refusing invalid yield manifest")
        for path in self.paths:
            folder = path.parent.name
            managed = self.root / folder
            if (
                folder not in _FOLDERS or path.parent != managed
                or managed.resolve() != managed
                or not self._matches(folder, path.name, self.video_id)
            ):
                raise RuntimeError(f"refusing yield outside managed folder or video: {path}")

    def remove(self, report: Callable[[str], None]) -> int:
        """Unlink every inspected file individually without globs or recursion.

        Example: each removed path receives its own activity-trace line.
        """

        self.validate()
        removed = 0
        for path in self.paths:
            report(f"Removing · {path}")
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            removed += 1
        return removed


class YieldItem(NamedTuple):
    """Keep a human title beside its exact removal manifest.

    Example: the confirmation lists a video's title followed by its file paths.
    """

    title: str
    yields: VideoYields


class YieldSelection(NamedTuple):
    """Capture the inspected filter snapshot and protected videos for confirmation.

    Example: changing filters later cannot add videos to this removal selection.
    """

    items: tuple[YieldItem, ...]
    skipped: tuple[str, ...] = ()
    size_bytes: int = 0

    @property
    def file_count(self) -> int:
        """Count the exact approved paths; e.g. six yields for each of two videos."""

        return sum(len(item.yields.paths) for item in self.items)


class RemovalResult(NamedTuple):
    """Report completed removals separately from videos protected during execution.

    Example: playback started after confirmation can increase `skipped`.
    """

    videos: int
    files: int
    skipped: int = 0
