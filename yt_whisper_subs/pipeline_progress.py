"""Structured, opt-in progress events shared by the CLI pipeline and native GUI.

Example: `emit(Stage.TRANSCRIBING, 0.5)` updates a GUI-launched run only.
"""

from __future__ import annotations

import json
import os
import re
from enum import StrEnum
from typing import NamedTuple


ENV_VIDEO_ID = "YT_WHISPER_SUBS_PROGRESS_VIDEO_ID"
PREFIX = "@@yt-whisper-progress "


class Stage(StrEnum):
    """Name stable pipeline phases and terminal catalog states.

    Example: `Stage.DOWNLOADING` identifies yt-dlp work.
    """

    AVAILABLE = "available"
    QUEUED = "queued"
    PREPARING = "preparing"
    DOWNLOADING = "downloading"
    EXTRACTING = "extracting"
    TRANSCRIBING = "transcribing"
    TRANSLATING = "translating"
    CHAPTERING = "chaptering"
    FINALIZING = "finalizing"
    READY = "ready"
    FAILED = "failed"
    CANCELLED = "cancelled"
    LIVE = "live"
    UPCOMING = "upcoming"


class StageSpec(NamedTuple):
    """Describe one weighted and colored segment of the pipeline bar.

    Example: `STAGE_SPECS[1].weight` sizes the download segment.
    """

    stage: Stage
    label: str
    weight: float
    color: str


STAGE_SPECS = (
    StageSpec(Stage.PREPARING, "Preparing", 0.03, "#8f9aaa"),
    StageSpec(Stage.DOWNLOADING, "Downloading", 0.32, "#4ea1f3"),
    StageSpec(Stage.EXTRACTING, "Extracting audio", 0.07, "#55d6be"),
    StageSpec(Stage.TRANSCRIBING, "Speech-to-text", 0.30, "#a78bfa"),
    StageSpec(Stage.TRANSLATING, "Translating", 0.16, "#f3bd63"),
    StageSpec(Stage.CHAPTERING, "Creating chapters", 0.07, "#f27bbd"),
    StageSpec(Stage.FINALIZING, "Finalizing", 0.05, "#58d68d"),
)
_WORK_SPECS = {spec.stage: spec for spec in STAGE_SPECS}
_DOWNLOAD_PERCENT = re.compile(r"\[download\]\s+(?P<pct>\d{1,3}(?:\.\d+)?)%", re.IGNORECASE)
_WHISPER_PERCENT = re.compile(r"(?P<pct>\d{1,3})%\|[^|]*\|")


class Update(NamedTuple):
    """Carry one video-scoped stage update across the subprocess boundary.

    Example: `Update("id", Stage.DOWNLOADING, .4, "Downloading")`.
    """

    video_id: str
    stage: Stage
    fraction: float | None
    label: str


_last_emitted: Update | None = None


def make(video_id: str, stage: Stage, fraction: float | None = None, label: str | None = None) -> Update:
    """Build a normalized update with a consistent default stage label.

    Example: `make("id", Stage.QUEUED)` labels the row `Queued`.
    """

    normalized = None if fraction is None else min(1.0, max(0.0, float(fraction)))
    return Update(video_id, stage, normalized, label or stage_label(stage))


def stage_label(stage: Stage) -> str:
    """Return concise human wording for a pipeline or catalog state.

    Example: `stage_label(Stage.TRANSCRIBING)` returns `Speech-to-text`.
    """

    labels = {
        Stage.AVAILABLE: "Available",
        Stage.QUEUED: "Queued",
        Stage.READY: "Ready to play",
        Stage.FAILED: "Failed",
        Stage.CANCELLED: "Cancelled",
        Stage.LIVE: "Live now",
        Stage.UPCOMING: "Upcoming",
    }
    if spec := _WORK_SPECS.get(stage):
        return spec.label
    return labels[stage]


def encode(update: Update) -> str:
    """Serialize an update as one recognizable stdout record.

    Example: `encode(update)` is safe to mix with normal command output.
    """

    payload = {
        "video_id": update.video_id,
        "stage": update.stage.value,
        "fraction": update.fraction,
        "label": update.label,
    }
    return PREFIX + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def parse(message: str) -> Update | None:
    """Decode a progress record while ignoring ordinary trace output.

    Example: `parse("ffmpeg output")` returns `None`.
    """

    if not message.startswith(PREFIX):
        return None
    try:
        payload = json.loads(message.removeprefix(PREFIX))
        return make(
            str(payload["video_id"]),
            Stage(payload["stage"]),
            payload.get("fraction"),
            str(payload["label"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def emit(stage: Stage, fraction: float | None = None, label: str | None = None) -> None:
    """Print an event only when a GUI parent requested structured progress.

    Example: normal CLI runs call `emit(...)` without changing their output.
    """

    global _last_emitted
    video_id = os.environ.get(ENV_VIDEO_ID)
    if not video_id:
        return
    _last_emitted = make(video_id, stage, fraction, label)
    print(encode(_last_emitted), flush=True)


def emit_failure(error: object) -> None:
    """Publish a terminal failure while retaining the reached bar position.

    Example: the CLI exception boundary calls `emit_failure(exc)`.
    """

    video_id = os.environ.get(ENV_VIDEO_ID)
    if not video_id:
        return
    reached = overall_fraction(_last_emitted) if _last_emitted else 0.0
    detail = str(error).strip()
    label = f"Failed · {detail}" if detail else "Failed"
    print(encode(make(video_id, Stage.FAILED, reached, label)), flush=True)


def overall_fraction(update: Update | None) -> float:
    """Map a phase-local fraction onto the segmented overall bar.

    Example: completed download work reaches the extraction segment boundary.
    """

    if not update:
        return 0.0
    if update.stage is Stage.READY:
        return 1.0
    if update.stage in {Stage.FAILED, Stage.CANCELLED}:
        return update.fraction or 0.0
    if update.stage not in _WORK_SPECS:
        return 0.0

    start = 0.0
    for spec in STAGE_SPECS:
        if spec.stage is update.stage:
            local = update.fraction if update.fraction is not None else 0.08
            return min(1.0, start + spec.weight * local)
        start += spec.weight
    return 0.0


def active(update: Update | None) -> bool:
    """Distinguish live work from static and terminal row states.

    Example: `active(make(id, Stage.DOWNLOADING))` is true.
    """

    return bool(update and update.stage in {Stage.QUEUED, *_WORK_SPECS})


def derive_tool_update(message: str, current: Update | None) -> Update | None:
    """Recover trustworthy yt-dlp and Whisper percentages from tool output.

    Example: a `[download] 25%` record advances the download segment.
    """

    if not current:
        return None
    match = _DOWNLOAD_PERCENT.search(message) if current.stage is Stage.DOWNLOADING else None
    if match:
        fraction = float(match.group("pct")) / 100
        return make(current.video_id, current.stage, fraction, f"Downloading · {fraction:.0%}")
    if current.stage not in {Stage.TRANSCRIBING, Stage.TRANSLATING}:
        return None
    match = _WHISPER_PERCENT.search(message)
    if not match:
        return None
    fraction = float(match.group("pct")) / 100
    action = "Speech-to-text" if current.stage is Stage.TRANSCRIBING else "Translating speech"
    return make(current.video_id, current.stage, fraction, f"{action} · {fraction:.0%}")
