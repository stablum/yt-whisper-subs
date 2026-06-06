"""Local media path, naming, and audio extraction helpers.

Example: `media.extract_audio(video, audio, "opus", force=False)`.
"""

from __future__ import annotations

import re
from pathlib import Path

from yt_whisper_subs import proc


def resolve_video_path(video_file: str) -> Path:
    """Resolve and validate a user-provided local video path.

    Example: `resolve_video_path("clip.mkv")`.
    """

    video_path = Path(video_file).expanduser().resolve()
    if not video_path.exists():
        raise RuntimeError(f"video file not found: {video_path}")
    return video_path


def safe_output_stem(value: str, *, fallback: str = "run") -> str:
    """Make a short Windows-friendly filename stem for generated logs.

    Example: `safe_output_stem("Video: title")`.
    """

    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip()).strip("._-")
    return cleaned[:100] or fallback


def audio_codec_args(audio_format: str) -> list[str]:
    """Map the selected kept-audio format to ffmpeg codec arguments.

    Example: `audio_codec_args("opus")`.
    """

    if audio_format == "opus":
        return ["-c:a", "libopus", "-b:a", "48k", "-vbr", "on"]
    if audio_format == "m4a":
        return ["-c:a", "aac", "-b:a", "64k"]
    if audio_format == "mp3":
        return ["-c:a", "libmp3lame", "-b:a", "64k"]
    raise ValueError(f"unsupported audio format: {audio_format}")


def extract_audio(video_path: Path, audio_path: Path, audio_format: str, force: bool) -> None:
    """Extract compact mono speech audio for Whisper.

    Example: `extract_audio(video, audio, "opus", force=False)`.
    """

    if audio_path.exists() and not force:
        return

    audio_path.parent.mkdir(parents=True, exist_ok=True)
    proc.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-y",
            "-i",
            video_path,
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            *audio_codec_args(audio_format),
            audio_path,
        ]
    )
