"""Share video resolution policy between native settings and the CLI.

Example: `VideoPrefs(1080, True).format_selector()` prefers full HD.
"""

from __future__ import annotations

from typing import NamedTuple

from yt_whisper_subs import cfg


class VideoPrefs(NamedTuple):
    """Keep the preferred resolution and its fallback policy together.

    Example: `VideoPrefs(720, False)` caps new downloads at 720p.
    """

    resolution: int = cfg.DEFAULT_VIDEO_RESOLUTION
    allow_higher: bool = cfg.DEFAULT_ALLOW_HIGHER_RESOLUTION

    def format_selector(self) -> str:
        """Try the preferred height, next lower preset, higher, then remaining lower.

        Example: 1080p -> 720p -> above 1080p -> remaining lower heights.
        """

        best = "bv*+ba/b"
        if not self.resolution:
            return best
        filters = [f"[height={self.resolution}]"]
        next_lower = next(
            (res for res in reversed(cfg.VIDEO_RESOLUTIONS) if 0 < res < self.resolution),
            None,
        )
        if next_lower is not None:
            filters.append(f"[height={next_lower}]")
        if self.allow_higher:
            filters.append(f"[height>{self.resolution}]")
        filters.append(f"[height<{self.resolution}]")
        choices = [f"bv*{height}+ba/b{height}" for height in filters]
        if self.allow_higher:
            # Unreported heights are a last resort; a strict ceiling cannot verify them.
            choices.append(best)
        return "/".join(choices)

    def command_args(self) -> list[str]:
        """Pass the same policy to a library-owned CLI pipeline.

        Example: disabling fallback emits `--no-allow-higher-resolution`.
        """

        fallback = "--allow-higher-resolution" if self.allow_higher else "--no-allow-higher-resolution"
        return ["--video-resolution", str(self.resolution), fallback]
