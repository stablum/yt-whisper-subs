"""Structured mpv progress events shared by playback, persistence, and Qt.

Example: `encode(make("id", 30, 120))` crosses the worker signal boundary.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from typing import NamedTuple

from yt_whisper_subs import cfg

PREFIX = "@@yt-whisper-playback "


class WatchPolicy(NamedTuple):
    """Keep the configurable strict watched boundary shared across consumers.

    Example: `WatchPolicy(90).apply(update)` marks progress above 90 percent.
    """

    percent: float = cfg.DEFAULT_LIBRARY_WATCHED_PERCENT

    @classmethod
    def from_percent(cls, value: str | float) -> WatchPolicy:
        """Reject invalid saved percentages in favor of the shipped default.

        Example: a damaged SQLite setting cannot classify every video as watched.
        """

        try:
            percent = float(value)
        except (TypeError, ValueError):
            return cls()
        return cls(percent) if math.isfinite(percent) and 1 <= percent <= 100 else cls()

    def apply(self, update: Update) -> Update:
        """Classify an observation without fabricating playback progress.

        Example: an explicit completion at 20 percent remains 20 percent.
        """

        duration = update.duration_seconds
        reached = bool(duration and update.position_seconds > duration * self.percent / 100)
        return update._replace(completed=update.completed or reached)


class Update(NamedTuple):
    """Carry one video-scoped playback observation through the application.

    Example: `Update("id", 30, 120, False)` represents 25 percent.
    """

    video_id: str
    position_seconds: float
    duration_seconds: float | None
    completed: bool

    def normalized(self) -> Update:
        """Clamp times while preserving the sender's completion decision.

        Example: decoding a 96-percent update must not reapply the default threshold.
        """

        position = max(0.0, float(self.position_seconds))
        duration = self.duration_seconds
        duration = None if duration is None else max(0.0, float(duration))
        return self._replace(position_seconds=position, duration_seconds=duration, completed=bool(self.completed))


class Observer(NamedTuple):
    """Bind a catalog video ID to its playback-update consumer.

    Example: `Observer(video_id, db.record_playback)` enables tracking.
    """

    video_id: str
    callback: Callable[[Update], None]
    policy: Callable[[], WatchPolicy] = WatchPolicy

    def publish(
        self,
        position_seconds: float,
        duration_seconds: float | None,
        *,
        completed: bool = False,
    ) -> None:
        """Normalize and deliver one observation to the bound consumer.

        Example: `observer.publish(60, 120)` records halfway progress.
        """

        update = make(self.video_id, position_seconds, duration_seconds, completed, policy=self.policy())
        self.callback(update)


def make(
    video_id: str,
    position_seconds: float,
    duration_seconds: float | None,
    completed: bool = False,
    *,
    policy: WatchPolicy = WatchPolicy(),
) -> Update:
    """Normalize mpv times and mark progress beyond the watched threshold.

    Example: `make("id", 96, 100).completed` is `True` before EOF.
    """

    update = Update(video_id, position_seconds, duration_seconds, completed).normalized()
    return policy.apply(update)


def fraction(update: Update) -> float:
    """Measure observed progress independently of the watched classification.

    Example: `fraction(make("id", 98, 100))` is `0.98` even when watched.
    """

    if not update.duration_seconds:
        return 0.0
    raw = update.position_seconds / update.duration_seconds
    return min(1.0, max(0.0, raw))


def encode(update: Update) -> str:
    """Serialize playback progress as one recognizable worker message.

    Example: `encode(update)` can share a signal with human trace output.
    """

    payload = {
        "video_id": update.video_id,
        "position_seconds": update.position_seconds,
        "duration_seconds": update.duration_seconds,
        "completed": update.completed,
    }
    return PREFIX + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def parse(message: str) -> Update | None:
    """Decode playback progress while ignoring ordinary task messages.

    Example: `parse("Playing video")` returns `None`.
    """

    if not message.startswith(PREFIX):
        return None
    try:
        payload = json.loads(message.removeprefix(PREFIX))
        return Update(
            str(payload["video_id"]),
            float(payload["position_seconds"]),
            float(payload["duration_seconds"])
            if payload.get("duration_seconds") is not None
            else None,
            bool(payload["completed"]),
        ).normalized()
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
