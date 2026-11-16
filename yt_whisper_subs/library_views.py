"""Define stable identities and presentation text for catalog smart views.

Example: `VIDEO_VIEWS` directly generates the native filter chips.
"""

from __future__ import annotations

import enum
from typing import NamedTuple


from yt_whisper_subs import i18n
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
    REMOVED = 8

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
    VideoViewSpec(VideoView.ALL, i18n.mark('All'), i18n.mark('Every video in the current library or channel')),
    VideoViewSpec(VideoView.ON_DEVICE, i18n.mark('On device'), i18n.mark('Downloaded videos ready to play')),
    VideoViewSpec(VideoView.AVAILABLE, i18n.mark('Available'), i18n.mark('Videos without a local download, including removed videos')),
    VideoViewSpec(VideoView.REMOVED, i18n.mark('Removed'), i18n.mark('Videos whose local video and yields were deliberately removed')),
    VideoViewSpec(VideoView.PIPELINE, i18n.mark('Pipeline'), i18n.mark('Videos queued or currently being processed')),
    VideoViewSpec(VideoView.UNWATCHED, i18n.mark('Unwatched'), i18n.mark('Current or previous downloads not watched yet')),
    VideoViewSpec(VideoView.CONTINUE, i18n.mark('Continue'), i18n.mark('Started videos still unfinished')),
    VideoViewSpec(VideoView.WATCHED, i18n.mark('Watched'), i18n.mark('Videos marked watched manually, by the configured threshold, or at the end')),
    VideoViewSpec(VideoView.ISSUES, i18n.mark('Issues'), i18n.mark('Videos whose latest download or processing attempt failed')),
)
