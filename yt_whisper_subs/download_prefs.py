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
        """Prefer an exact height, or enforce a ceiling when higher is disabled.

        Example: 1080p beats 4K; absent 1080p can still fall back to 4K.
        """

        best = "bv*+ba/b"
        if not self.resolution:
            return best
        comparison = "=" if self.allow_higher else "<="
        height = f"[height{comparison}{self.resolution}]"
        preferred = f"bv*{height}+ba/b{height}"
        return f"{preferred}/{best}" if self.allow_higher else preferred

    def command_args(self) -> list[str]:
        """Pass the same policy to a library-owned CLI pipeline.

        Example: disabling fallback emits `--no-allow-higher-resolution`.
        """

        fallback = "--allow-higher-resolution" if self.allow_higher else "--no-allow-higher-resolution"
        return ["--video-resolution", str(self.resolution), fallback]
