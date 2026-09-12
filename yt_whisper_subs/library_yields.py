"""Exact, non-recursive inventory and removal of one library video's yields.

Example: `VideoYields.inspect(root, video_id).remove(report)` deletes one manifest.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple


_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")


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

        if not _VIDEO_ID.fullmatch(video_id):
            raise ValueError(f"invalid YouTube video ID: {video_id!r}")
        resolved_root = root.resolve()
        paths: list[Path] = []
        for folder in ("videos", "audio", "metadata", "subtitles", "chapters", "logs"):
            directory = (resolved_root / folder).resolve()
            if not directory.is_dir():
                continue
            for path in directory.iterdir():
                if not (path.is_file() or path.is_symlink()):
                    continue
                if not cls._matches(folder, path.name, video_id):
                    continue
                absolute = path.absolute()
                if not absolute.is_relative_to(directory):
                    raise RuntimeError(f"refusing yield outside managed folder: {path}")
                paths.append(absolute)
        return cls(resolved_root, video_id, tuple(sorted(paths, key=lambda path: str(path).casefold())))

    @staticmethod
    def _matches(folder: str, name: str, video_id: str) -> bool:
        """Require an ID delimiter and expected log suffix before inclusion.

        Example: `abcdefghijk2.srt` never matches ID `abcdefghijk`.
        """

        if folder == "logs":
            return name.startswith(f"{video_id}-") and name.casefold().endswith(".log")
        return name.startswith(f"{video_id}.")

    def remove(self, report: Callable[[str], None]) -> int:
        """Unlink every inspected file individually without globs or recursion.

        Example: each removed path receives its own activity-trace line.
        """

        removed = 0
        for path in self.paths:
            report(f"Removing · {path}")
            path.unlink(missing_ok=True)
            removed += 1
        return removed
