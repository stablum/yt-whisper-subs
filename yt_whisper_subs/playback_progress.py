"""Structured mpv progress events shared by playback, persistence, and Qt.

Example: `encode(make("id", 30, 120))` crosses the worker signal boundary.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import NamedTuple


PREFIX = "@@yt-whisper-playback "


class Update(NamedTuple):
    """Carry one video-scoped playback observation through the application.

    Example: `Update("id", 30, 120, False)` represents 25 percent.
    """

    video_id: str
    position_seconds: float
    duration_seconds: float | None
    completed: bool


class Observer(NamedTuple):
    """Bind a catalog video ID to its playback-update consumer.

    Example: `Observer(video_id, db.record_playback)` enables tracking.
    """

    video_id: str
    callback: Callable[[Update], None]

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

        self.callback(make(self.video_id, position_seconds, duration_seconds, completed))


def make(
    video_id: str,
    position_seconds: float,
    duration_seconds: float | None,
    completed: bool = False,
) -> Update:
    """Normalize raw mpv time values into a safe progress update.

    Example: `make("id", -2, 100)` clamps the position to zero.
    """

    position = max(0.0, float(position_seconds))
    duration = None if duration_seconds is None else max(0.0, float(duration_seconds))
    return Update(video_id, position, duration, bool(completed))


def fraction(update: Update) -> float:
    """Calculate progress while reserving 100 percent for confirmed EOF.

    Example: `fraction(make("id", 100, 100))` is `0.99` without EOF.
    """

    if update.completed:
        return 1.0
    if not update.duration_seconds:
        return 0.0
    raw = update.position_seconds / update.duration_seconds
    return min(0.99, max(0.0, raw))


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
        return make(
            str(payload["video_id"]),
            float(payload["position_seconds"]),
            float(payload["duration_seconds"])
            if payload.get("duration_seconds") is not None
            else None,
            bool(payload["completed"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
