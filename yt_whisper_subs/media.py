"""Local media path, naming, and audio extraction helpers.

Example: `media.extract_audio(video, audio, "opus", force=False)`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from yt_whisper_subs import proc
from yt_whisper_subs import yield_files


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
    """Promote complete audio only after extraction and duration validation.

    Example: `extract_audio(video, audio, "opus", force=False)`.
    """

    source_stamp = _file_stamp(video_path)
    if not force and _audio_is_current(audio_path, source_stamp):
        return

    with yield_files.staged_path(audio_path) as stage:
        proc.run([
            "ffmpeg", "-hide_banner", "-y", "-i", video_path,
            "-vn", "-ac", "1", "-ar", "16000",
            *audio_codec_args(audio_format), stage,
        ])
        source_duration = probe_audio_duration_ms(video_path)
        audio_duration = probe_duration_ms(stage)
        if not audio_duration or stage.stat().st_size == 0:
            raise RuntimeError("extracted audio could not be validated; previous audio was preserved")
        tolerance = max(500, round((source_duration or audio_duration) * 0.02))
        if source_duration and abs(audio_duration - source_duration) > tolerance:
            raise RuntimeError("extracted audio duration does not match the video; previous audio was preserved")
        if _file_stamp(video_path) != source_stamp:
            raise RuntimeError("source video changed during extraction; audio was not replaced")
        stage.replace(audio_path)
    provenance = {"source": source_stamp, "audio": _file_stamp(audio_path)}
    yield_files.atomic_write(_audio_provenance(audio_path), json.dumps(provenance))


def _file_stamp(path: Path) -> list[str | int]:
    """Bind a validated cache to its exact source and output file revisions.

    Example: replacing a short replay changes its size or modification time.
    """

    stat = path.stat()
    return [str(path.resolve()), stat.st_size, stat.st_mtime_ns]


def _audio_provenance(audio_path: Path) -> Path:
    """Keep validation evidence beside the extracted audio cache.

    Example: `video.opus.source.json` is included in exact-ID cleanup.
    """

    return audio_path.with_name(f"{audio_path.name}.source.json")


def _audio_is_current(audio_path: Path, source_stamp: list[str | int]) -> bool:
    """Reuse only a completed cache whose source and audio are unchanged.

    Example: an unvalidated or externally truncated audio file is extracted again.
    """

    try:
        document = json.loads(_audio_provenance(audio_path).read_text(encoding="utf-8"))
        expected = {"source": source_stamp, "audio": _file_stamp(audio_path)}
        return audio_path.stat().st_size > 0 and document == expected
    except (OSError, ValueError):
        return False


def remove_audio(audio_path: Path) -> None:
    """Discard a cache and its validation evidence together.

    Example: the pipeline calls this for `--delete-audio`.
    """

    audio_path.unlink(missing_ok=True)
    _audio_provenance(audio_path).unlink(missing_ok=True)


def probe_audio_duration_ms(video_path: Path) -> int | None:
    """Read the source audio stream length independently of silent video tails.

    Example: a 90-second video may legitimately contain a 20-second audio track.
    """

    proc.require_command("ffprobe")
    result = proc.run([
        "ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=duration:stream_tags=DURATION", "-of", "json", video_path,
    ], capture_stdout=True)
    try:
        streams = json.loads(result.stdout or "{}").get("streams", [])
        if not streams:
            return None
        stream = streams[0]
        seconds = stream.get("duration")
        if seconds is None or seconds == "N/A":
            parts = stream.get("tags", {}).get("DURATION", "").split(":")
            if len(parts) != 3:
                return None
            hours, minutes, seconds = map(float, parts)
            seconds += 3600 * hours + 60 * minutes
        duration = round(float(seconds) * 1000)
        return duration if duration > 0 else None
    except (ValueError, TypeError, AttributeError, KeyError):
        return None


def probe_duration_ms(video_path: Path) -> int | None:
    """Read container duration with ffprobe, falling back cleanly on unknowns.

    Example: `probe_duration_ms(video)` supplies chapter-density math.
    """

    proc.require_command("ffprobe")
    result = proc.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            video_path,
        ],
        capture_stdout=True,
    )
    try:
        duration_ms = round(float((result.stdout or "").strip()) * 1000)
    except ValueError:
        return None
    return duration_ms if duration_ms > 0 else None
