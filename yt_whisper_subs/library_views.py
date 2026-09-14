"""Define stable identities and presentation text for catalog smart views.

Example: `VIDEO_VIEWS` directly generates the native filter chips.
"""

from __future__ import annotations

import enum
from typing import NamedTuple


class VideoView(enum.IntEnum):
    """Name each mutually exclusive task-oriented catalog view.

    Example: `VideoView.PIPELINE` selects queued and active work.
    """

    ALL = 0
    ON_DEVICE = 1
    AVAILABLE = 2
    UNWATCHED = 3
    CONTINUE = 4
    WATCHED = 5
    ISSUES = 6
    PIPELINE = 7

    @property
    def key(self) -> str:
        """Return the stable lowercase value persisted in library settings.

        Example: `VideoView.ON_DEVICE.key` is `"on_device"`.
        """

        return self.name.lower()

    @classmethod
    def from_key(cls, key: str) -> VideoView:
        """Restore a persisted key, falling back safely to the full catalog.

        Example: `VideoView.from_key("watched")` restores that smart view.
        """

        try:
            return cls[key.upper()]
        except KeyError:
            return cls.ALL


class VideoViewSpec(NamedTuple):
    """Keep each smart view's wording and explanation beside its identity.

    Example: filter chips are generated directly from `VIDEO_VIEWS`.
    """

    view: VideoView
    label: str
    tooltip: str


VIDEO_VIEWS = (
    VideoViewSpec(VideoView.ALL, "All", "Every video in the current library or channel"),
    VideoViewSpec(VideoView.ON_DEVICE, "On device", "Downloaded videos ready to play"),
    VideoViewSpec(VideoView.AVAILABLE, "Available", "Tracked videos not downloaded yet"),
    VideoViewSpec(VideoView.PIPELINE, "Pipeline", "Videos queued or currently being processed"),
    VideoViewSpec(VideoView.UNWATCHED, "Unwatched", "Downloaded videos not started yet"),
    VideoViewSpec(VideoView.CONTINUE, "Continue", "Started videos that have not reached the end"),
    VideoViewSpec(VideoView.WATCHED, "Watched", "Videos confirmed complete by mpv"),
    VideoViewSpec(VideoView.ISSUES, "Issues", "Videos whose latest download or processing attempt failed"),
)
