#!/usr/bin/env python
"""Download a video, generate local Whisper subtitles, and optionally play it.

This script does not depend on YouTube captions. It downloads a compressed
YouTube video with yt-dlp or accepts a local video file, extracts small lossy
16 kHz mono audio with ffmpeg, runs OpenAI Whisper locally, writes an SRT file,
and can open the video in mpv with the generated subtitle file attached. The
primary SRT is written as a sidecar next to the video so mpv can auto-detect it
on later opens. For Dutch sources, the default English translation path sends
indexed cue text to the OpenAI API in bounded chunks and preserves primary cue
timings.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import parse_qs, urlparse


MODEL_CHOICES = (
    "tiny",
    "base",
    "small",
    "medium",
    "large",
    "large-v2",
    "large-v3",
    "turbo",
)
DEFAULT_OUTPUT_DIR = Path.home() / "Videos" / "yt-whisper-subs"
DEFAULT_OPENAI_ENV_FILE = Path(__file__).resolve().parent / ".env"
DEFAULT_OPENAI_TRANSLATION_MODEL = "gpt-5-mini"
DEFAULT_OPENAI_TRANSLATION_REASONING = "low"
DEFAULT_OPENAI_TIMEOUT = 900.0
DEFAULT_OPENAI_MAX_RETRIES = 3
DEFAULT_OPENAI_RETRY_INITIAL_DELAY = 5.0
GPT_5_MINI_REASONING_EFFORTS = ("minimal", "low", "medium", "high")
DEFAULT_OPENAI_TRANSLATION_CHUNK_CUES = 120
DEFAULT_OPENAI_TRANSLATION_CONTEXT_CUES = 3
DEFAULT_PYTHON_VERSION = "3.14"
DEFAULT_TORCH_INDEX_URL = "https://download.pytorch.org/whl/cu128"
DEFAULT_DUAL_SUB_PRIMARY_COLOR = "#FFE066"
DEFAULT_DUAL_SUB_SECONDARY_COLOR = "#66D9EF"
DEFAULT_DUAL_SUB_PRIMARY_POS = 100
DEFAULT_DUAL_SUB_SECONDARY_POS = 8
DEFAULT_DUAL_SUB_FONT_SIZE = 80
DEFAULT_DUAL_SUB_PRIMARY_FONT_SCALE = 0.6
DEFAULT_DOWNLOAD_PROGRESS_DELTA = 1.0
DEFAULT_COMPACT_GAP = 0.9
DEFAULT_COMPACT_MAX_DURATION = 9.0
DEFAULT_COMPACT_MAX_CHARS = 180
DEFAULT_COMPACT_MAX_CPS = 25.0
DEFAULT_COMPACT_LINE_WIDTH = 50
DEFAULT_SUBTITLE_GAP_EXTENSION = 5.0
DEFAULT_COMPACT_SOFT_PERIODS = "english"
AUDIO_FORMAT_CHOICES = ("opus", "m4a", "mp3")
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
INTERMEDIATE_FORMAT_RE = re.compile(r"\.f\d+\.(?:m4a|mkv|mp4|webm)$", re.IGNORECASE)
TRANSIENT_OPENAI_HTTP_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}
TRANSIENT_OPENAI_NETWORK_ERRORS = (
    urlerror.URLError,
    http.client.HTTPException,
    ConnectionError,
    TimeoutError,
    OSError,
)
SRT_TIME_RE = re.compile(
    r"(?P<start>\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(?P<end>\d{2}:\d{2}:\d{2},\d{3})"
)
STRONG_PUNCTUATION_RE = re.compile(r"[.!?][\"')\]]*$")
YOUTUBE_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
TERMINAL_PERIOD_RE = re.compile(r"\.(?P<trailer>[\"')\]]*)$")
WORD_RE = re.compile(r"[A-Za-z][A-Za-z']*")
FALSE_PERIOD_END_WORDS = frozenset(
    """
    a about after against although among an and are around as at be because
    been before being between but by can could did do does during for from
    had has have how if in into is may might must of on or shall should since
    so than that the through to under unless until was were what when
    where whether which while who whom whose why will with within without
    would
    """.split()
)
FALSE_PERIOD_START_WORDS = frozenset(
    """
    about after although and are as at because been before being but by can
    could did do does for from had has have how if in into is not of on or
    shall should since so than that through to under unless until was were what
    when where whether which while who whom whose why will with within without
    would
    """.split()
)
COORDINATING_SOFT_PERIOD_START_WORDS = frozenset("and but or so".split())
LOWERCASE_AFTER_SOFT_PERIOD_WORDS = FALSE_PERIOD_START_WORDS | frozenset(
    """
    a an the their them there these they this those it its our we you your he
    her his she
    """.split()
)


@dataclass(frozen=True)
class SubtitleCue:
    start_ms: int
    end_ms: int
    text: str


@dataclass(frozen=True)
class OpenAITranslationParseResult:
    texts: list[str | None]
    errors: list[str]

    @property
    def missing_indexes(self) -> list[int]:
        return [index + 1 for index, text in enumerate(self.texts) if text is None]

    @property
    def complete(self) -> bool:
        return not self.missing_indexes


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download a YouTube video or use a local video, generate an SRT "
            "with local Whisper, and optionally play it in mpv."
        )
    )
    parser.add_argument(
        "source",
        nargs="?",
        help="YouTube/video URL or local video file path. Use --url or --video-file for clarity.",
    )

    source_group = parser.add_mutually_exclusive_group()
    source_group.add_argument("--url", help="YouTube/video URL to download with yt-dlp.")
    source_group.add_argument("--video-file", help="Local video file to subtitle.")

    parser.add_argument(
        "--out-dir",
        help=f"Output root directory. Default: {DEFAULT_OUTPUT_DIR}",
    )
    parser.add_argument(
        "--language",
        default="nl",
        help="Whisper language code, or 'auto' to let Whisper detect it. Default: nl.",
    )
    parser.add_argument("--model", choices=MODEL_CHOICES, default="turbo")
    parser.add_argument(
        "--english-model",
        choices=MODEL_CHOICES,
        help=(
            "Whisper model for Dutch-to-English audio translation when "
            "--english-translation-provider whisper is used. Defaults to medium "
            "when --model is turbo, otherwise defaults to --model."
        ),
    )
    parser.add_argument(
        "--english-translation-provider",
        choices=("openai", "whisper"),
        default="openai",
        help=(
            "How Dutch-to-English subtitles are generated. The default, openai, sends the "
            "primary cue text to the OpenAI Responses API in bounded indexed chunks and preserves exact cue timings. "
            "Use whisper for the previous local audio translation path."
        ),
    )
    parser.add_argument(
        "--openai-translation-model",
        default=DEFAULT_OPENAI_TRANSLATION_MODEL,
        help=(
            "OpenAI model used for SRT English translation. "
            f"Default: {DEFAULT_OPENAI_TRANSLATION_MODEL}."
        ),
    )
    parser.add_argument(
        "--openai-reasoning-effort",
        choices=("none", "minimal", "low", "medium", "high", "xhigh"),
        default=DEFAULT_OPENAI_TRANSLATION_REASONING,
        help=(
            "OpenAI reasoning effort for SRT English translation. "
            f"Default: {DEFAULT_OPENAI_TRANSLATION_REASONING}."
        ),
    )
    parser.add_argument(
        "--openai-timeout",
        type=float,
        default=DEFAULT_OPENAI_TIMEOUT,
        help=f"Seconds to wait for the OpenAI translation request. Default: {DEFAULT_OPENAI_TIMEOUT:g}.",
    )
    parser.add_argument(
        "--openai-max-retries",
        type=int,
        default=DEFAULT_OPENAI_MAX_RETRIES,
        help=(
            "Retries for transient OpenAI network/server failures after the initial request. "
            f"Default: {DEFAULT_OPENAI_MAX_RETRIES}."
        ),
    )
    parser.add_argument(
        "--openai-translation-chunk-cues",
        type=int,
        default=DEFAULT_OPENAI_TRANSLATION_CHUNK_CUES,
        help=(
            "Maximum subtitle cues per OpenAI translation request. Use 0 for one full cue-list request. "
            f"Default: {DEFAULT_OPENAI_TRANSLATION_CHUNK_CUES}."
        ),
    )
    parser.add_argument(
        "--openai-translation-context-cues",
        type=int,
        default=DEFAULT_OPENAI_TRANSLATION_CONTEXT_CUES,
        help=(
            "Neighboring cues sent as context around each OpenAI translation chunk. "
            f"Default: {DEFAULT_OPENAI_TRANSLATION_CONTEXT_CUES}."
        ),
    )
    parser.add_argument(
        "--openai-env-file",
        default=str(DEFAULT_OPENAI_ENV_FILE),
        help=(
            "Optional .env file to load before calling OpenAI. "
            f"Default: {DEFAULT_OPENAI_ENV_FILE}."
        ),
    )
    parser.add_argument("--task", choices=("transcribe", "translate"), default="transcribe")
    parser.add_argument("--device", choices=("cuda", "cpu"), default="cuda")
    parser.add_argument(
        "--audio-format",
        choices=AUDIO_FORMAT_CHOICES,
        default="opus",
        help="Lossy audio format to keep for Whisper input. Default: opus.",
    )
    parser.add_argument(
        "--video-format",
        default="bv*+ba/b",
        help=(
            "yt-dlp format selector. The default stores YouTube's already-lossy "
            "compressed A/V streams without creating a lossless video."
        ),
    )
    parser.add_argument(
        "--merge-output-format",
        choices=("mkv", "mp4", "webm"),
        default="mkv",
        help="Container for downloaded video streams. Default: mkv.",
    )
    parser.add_argument(
        "--download-progress-delta",
        type=float,
        default=DEFAULT_DOWNLOAD_PROGRESS_DELTA,
        help=f"Minimum seconds between yt-dlp progress updates. Default: {DEFAULT_DOWNLOAD_PROGRESS_DELTA:g}.",
    )
    parser.add_argument(
        "--torch-index-url",
        default=DEFAULT_TORCH_INDEX_URL,
        help="PyTorch package index URL used when installing CUDA wheels.",
    )
    parser.add_argument(
        "--install-tools",
        action="store_true",
        help="Install/update ffmpeg and mpv via scoop when available.",
    )
    parser.add_argument(
        "--install-python-deps",
        action="store_true",
        help="Create/update .venv beside this script with uv and install torch, yt-dlp, and openai-whisper.",
    )
    parser.add_argument(
        "--python-version",
        default=DEFAULT_PYTHON_VERSION,
        help=(
            "Python version for uv-managed .venv creation. "
            f"Default: {DEFAULT_PYTHON_VERSION}."
        ),
    )
    parser.add_argument("--no-play", action="store_true", help="Only create subtitles; do not open mpv.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download URL videos and regenerate subtitles even if yields already exist.",
    )
    parser.add_argument(
        "--no-english-for-dutch",
        dest="english_for_dutch",
        action="store_false",
        default=True,
        help="Do not create an additional English .en.srt when --language is Dutch.",
    )
    parser.add_argument(
        "--no-dual-subs",
        dest="dual_subs",
        action="store_false",
        default=True,
        help="Load multiple subtitles as selectable tracks instead of displaying two at once in mpv.",
    )
    parser.add_argument(
        "--dual-sub-primary-color",
        default=DEFAULT_DUAL_SUB_PRIMARY_COLOR,
        help=(
            "Text color for the primary subtitle track when dual subtitles are shown, "
            f"as #RRGGBB or #RRGGBBAA. Default: {DEFAULT_DUAL_SUB_PRIMARY_COLOR}."
        ),
    )
    parser.add_argument(
        "--dual-sub-secondary-color",
        default=DEFAULT_DUAL_SUB_SECONDARY_COLOR,
        help=(
            "Text color for the secondary subtitle track when dual subtitles are shown, "
            f"as #RRGGBB or #RRGGBBAA. Default: {DEFAULT_DUAL_SUB_SECONDARY_COLOR}."
        ),
    )
    parser.add_argument(
        "--dual-sub-primary-pos",
        type=float,
        default=DEFAULT_DUAL_SUB_PRIMARY_POS,
        help=f"Subtitle position hint for the primary subtitles. Default: {DEFAULT_DUAL_SUB_PRIMARY_POS}.",
    )
    parser.add_argument(
        "--dual-sub-secondary-pos",
        type=float,
        default=DEFAULT_DUAL_SUB_SECONDARY_POS,
        help=f"Subtitle position hint for the secondary subtitles. Default: {DEFAULT_DUAL_SUB_SECONDARY_POS}.",
    )
    parser.add_argument(
        "--dual-sub-font-size",
        type=float,
        default=DEFAULT_DUAL_SUB_FONT_SIZE,
        help=(
            "Visual subtitle font-size target for dual subtitles. The primary mpv-native "
            "track is scaled to match the secondary ASS track. "
            f"Default: {DEFAULT_DUAL_SUB_FONT_SIZE}."
        ),
    )
    parser.add_argument(
        "--dual-sub-primary-font-size",
        type=float,
        help=(
            "Override the native mpv font size for the primary subtitle track. "
            "By default this is derived from --dual-sub-font-size."
        ),
    )
    parser.add_argument(
        "--dual-sub-secondary-font-size",
        type=float,
        help=(
            "Override the ASS font size for the secondary subtitle track. "
            "By default this is --dual-sub-font-size."
        ),
    )
    parser.add_argument(
        "--compact-subs",
        choices=("english", "all", "none"),
        default="english",
        help=(
            "Compact fragmented SRT cues after generation and on existing files. Default: english. "
            "When OpenAI English translation is enabled, the primary SRT is also compacted first "
            "so translation uses those exact cue timings."
        ),
    )
    parser.add_argument(
        "--no-compact-subs",
        dest="compact_subs",
        action="store_const",
        const="none",
        help="Disable subtitle compaction.",
    )
    parser.add_argument(
        "--compact-soft-periods",
        choices=("english", "all", "none"),
        default=DEFAULT_COMPACT_SOFT_PERIODS,
        help=(
            "Treat likely false period boundaries as mergeable during compaction. "
            f"Default: {DEFAULT_COMPACT_SOFT_PERIODS}."
        ),
    )
    parser.add_argument(
        "--no-compact-soft-periods",
        dest="compact_soft_periods",
        action="store_const",
        const="none",
        help="Do not merge across period boundaries during subtitle compaction.",
    )
    parser.add_argument(
        "--compact-gap",
        type=float,
        default=DEFAULT_COMPACT_GAP,
        help=f"Maximum cue gap in seconds that may be merged. Default: {DEFAULT_COMPACT_GAP}.",
    )
    parser.add_argument(
        "--compact-max-duration",
        type=float,
        default=DEFAULT_COMPACT_MAX_DURATION,
        help=f"Maximum merged cue duration in seconds. Default: {DEFAULT_COMPACT_MAX_DURATION}.",
    )
    parser.add_argument(
        "--compact-max-chars",
        type=int,
        default=DEFAULT_COMPACT_MAX_CHARS,
        help=f"Maximum merged cue text length. Default: {DEFAULT_COMPACT_MAX_CHARS}.",
    )
    parser.add_argument(
        "--compact-max-cps",
        type=float,
        default=DEFAULT_COMPACT_MAX_CPS,
        help=f"Maximum merged cue reading speed in characters per second. Default: {DEFAULT_COMPACT_MAX_CPS}.",
    )
    parser.add_argument(
        "--compact-line-width",
        type=int,
        default=DEFAULT_COMPACT_LINE_WIDTH,
        help=f"Target subtitle line width when rewriting compacted cues. Default: {DEFAULT_COMPACT_LINE_WIDTH}.",
    )
    parser.add_argument(
        "--subtitle-gap-extension",
        type=float,
        default=DEFAULT_SUBTITLE_GAP_EXTENSION,
        help=(
            "Seconds to extend each subtitle cue into following silence, capped by the next cue start. "
            f"Use 0 to disable. Default: {DEFAULT_SUBTITLE_GAP_EXTENSION:g}."
        ),
    )
    audio_group = parser.add_mutually_exclusive_group()
    audio_group.add_argument(
        "--keep-audio",
        dest="keep_audio",
        action="store_true",
        default=True,
        help="Keep the extracted lossy audio file. This is the default.",
    )
    audio_group.add_argument(
        "--delete-audio",
        dest="keep_audio",
        action="store_false",
        help="Delete the extracted audio after subtitles are generated.",
    )
    parser.add_argument(
        "--cookies-from-browser",
        help="Pass through to yt-dlp, for example: firefox, chrome, edge.",
    )

    args = parser.parse_args()
    explicit_sources = [value for value in (args.source, args.url, args.video_file) if value]
    if len(explicit_sources) != 1:
        parser.error("provide exactly one source: positional SOURCE, --url, or --video-file")
    if args.openai_max_retries < 0:
        parser.error("--openai-max-retries must be 0 or greater")
    if args.openai_translation_chunk_cues < 0:
        parser.error("--openai-translation-chunk-cues must be 0 or greater")
    if args.openai_translation_context_cues < 0:
        parser.error("--openai-translation-context-cues must be 0 or greater")
    if args.subtitle_gap_extension < 0:
        parser.error("--subtitle-gap-extension must be 0 or greater")
    if (
        args.openai_translation_model == "gpt-5-mini"
        and args.openai_reasoning_effort not in GPT_5_MINI_REASONING_EFFORTS
    ):
        allowed = ", ".join(GPT_5_MINI_REASONING_EFFORTS)
        parser.error(
            "--openai-reasoning-effort must be one of "
            f"{allowed} when --openai-translation-model is gpt-5-mini"
        )

    if args.source:
        if looks_like_url(args.source):
            args.url = args.source
        else:
            args.video_file = args.source

    return args


def looks_like_url(value: str) -> bool:
    lowered = value.lower()
    return lowered.startswith(("http://", "https://"))


def is_dutch_language(language: str | None) -> bool:
    if not language:
        return False
    return language.casefold() in {"nl", "dutch", "nederlands"}


def english_model(args: argparse.Namespace) -> str:
    if args.english_model:
        return args.english_model
    if args.model == "turbo":
        return "medium"
    return args.model


def uses_openai_english_translation(args: argparse.Namespace) -> bool:
    return getattr(args, "english_translation_provider", "openai") == "openai"


def should_compact_subtitles(args: argparse.Namespace, *, is_english: bool) -> bool:
    if args.compact_subs == "none":
        return False
    if is_english and uses_openai_english_translation(args):
        return False
    if not is_english and getattr(args, "compact_primary_for_openai_translation", False):
        return True
    if args.compact_subs == "all":
        return True
    return is_english


def should_soften_period_boundaries(args: argparse.Namespace, *, is_english: bool) -> bool:
    mode = getattr(args, "compact_soft_periods", DEFAULT_COMPACT_SOFT_PERIODS)
    if mode == "none":
        return False
    if mode == "all":
        return True
    return is_english


def venv_paths() -> dict[str, Path]:
    script_dir = Path(__file__).resolve().parent
    venv_dir = script_dir / ".venv"
    scripts_dir = venv_dir / ("Scripts" if os.name == "nt" else "bin")
    exe_suffix = ".exe" if os.name == "nt" else ""

    return {
        "script_dir": script_dir,
        "venv_dir": venv_dir,
        "python": scripts_dir / f"python{exe_suffix}",
        "whisper": scripts_dir / f"whisper{exe_suffix}",
    }


def command_text(cmd: list[str | os.PathLike[str]]) -> str:
    return " ".join(str(part) for part in cmd)


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def run(
    cmd: list[str | os.PathLike[str]],
    *,
    capture_stdout: bool = False,
    stream_stdout: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    print()
    print(f"> {command_text(cmd)}")
    if capture_stdout and stream_stdout:
        process = subprocess.Popen(
            [str(part) for part in cmd],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
        )
        captured_parts: list[str] = []
        assert process.stdout is not None
        try:
            while True:
                chunk = process.stdout.read(1)
                if not chunk:
                    break
                captured_parts.append(chunk)
                sys.stdout.write(chunk)
                if chunk in {"\n", "\r"}:
                    sys.stdout.flush()
            returncode = process.wait()
        except BaseException:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            raise
        result = subprocess.CompletedProcess(
            [str(part) for part in cmd],
            returncode,
            stdout="".join(captured_parts),
        )
        if check and result.returncode != 0:
            raise RuntimeError(f"command failed with exit code {result.returncode}: {cmd[0]}")
        return result

    stdout = subprocess.PIPE if capture_stdout else None
    result = subprocess.run(
        [str(part) for part in cmd],
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=stdout,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"command failed with exit code {result.returncode}: {cmd[0]}")
    return result


def require_command(name: str) -> None:
    if shutil.which(name) is None:
        raise RuntimeError(f"required command not found: {name}")


def install_tools() -> None:
    if shutil.which("scoop") is None:
        raise RuntimeError("scoop not found. Install uv, ffmpeg, and mpv manually, or install scoop first.")
    run(["scoop", "install", "uv", "ffmpeg", "mpv"])
    run(["scoop", "update", "uv", "ffmpeg", "mpv"])


def get_python_minor_version(python_exe: Path) -> str | None:
    if not python_exe.exists():
        return None

    result = subprocess.run(
        [str(python_exe), "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def requested_python_minor_version(version: str) -> str:
    parts = version.split(".")
    if len(parts) < 2:
        return version
    return ".".join(parts[:2])


def ensure_python_deps(paths: dict[str, Path], args: argparse.Namespace) -> None:
    requested_minor = requested_python_minor_version(args.python_version)
    current_minor = get_python_minor_version(paths["python"])
    needs_python_deps = (
        args.install_python_deps
        or not paths["python"].exists()
        or current_minor != requested_minor
    )

    if needs_python_deps:
        require_command("uv")
        print(f"Creating/updating Python venv in: {paths['venv_dir']}")

        if paths["venv_dir"].exists() and current_minor != requested_minor:
            current_label = current_minor or "unknown"
            print(f"Recreating .venv with Python {args.python_version}; existing Python is {current_label}.")
            run(["uv", "venv", "--python", args.python_version, "--clear", paths["venv_dir"]])
        elif not paths["python"].exists():
            run(["uv", "venv", "--python", args.python_version, paths["venv_dir"]])

        run(["uv", "pip", "install", "--python", paths["python"], "--upgrade", "wheel", "setuptools"])
        run(["uv", "pip", "install", "--python", paths["python"], "--upgrade", "yt-dlp", "openai-whisper"])

        torch_cmd = [
            "uv",
            "pip",
            "install",
            "--python",
            paths["python"],
            "--upgrade",
        ]
        if args.device == "cuda":
            # CPU and CUDA wheels can satisfy the same "torch" requirement.
            # Force the wheel variant to be replaced when CUDA is requested.
            torch_cmd += ["--reinstall-package", "torch", "--index-url", args.torch_index_url]
        torch_cmd += ["torch"]
        run(torch_cmd)

    if not paths["python"].exists():
        raise RuntimeError(f"Whisper .venv not found at {paths['venv_dir']}. Re-run with --install-python-deps.")
    if not paths["whisper"].exists():
        raise RuntimeError("Whisper executable not found. Re-run with --install-python-deps.")


def check_cuda(paths: dict[str, Path]) -> bool:
    code = (
        "import torch\n"
        "print('cuda_available=' + str(torch.cuda.is_available()))\n"
        "print('device_count=' + str(torch.cuda.device_count()))\n"
        "print('device_name=' + (torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'NONE'))\n"
    )
    result = run([paths["python"], "-c", code], capture_stdout=True)
    lines = (result.stdout or "").splitlines()
    for line in lines:
        print(line)
    return "cuda_available=True" in lines


def resolve_video_path(video_file: str) -> Path:
    video_path = Path(video_file).expanduser().resolve()
    if not video_path.exists():
        raise RuntimeError(f"video file not found: {video_path}")
    return video_path


def clean_output_line(line: str) -> str:
    return ANSI_ESCAPE_RE.sub("", line).strip().strip('"')


def normalize_youtube_video_id(value: str | None) -> str | None:
    if not value:
        return None

    candidate = value.strip().strip("/")
    if YOUTUBE_VIDEO_ID_RE.fullmatch(candidate):
        return candidate
    return None


def youtube_video_id(url: str) -> str | None:
    parsed = urlparse(url)
    host = parsed.netloc.casefold()
    if host.startswith("www."):
        host = host[4:]

    path_parts = [part for part in parsed.path.split("/") if part]
    if host == "youtu.be":
        return normalize_youtube_video_id(path_parts[0] if path_parts else None)

    youtube_hosts = {
        "youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtube-nocookie.com",
    }
    if host not in youtube_hosts:
        return None

    query_video_ids = parse_qs(parsed.query).get("v", [])
    query_video_id = normalize_youtube_video_id(query_video_ids[0] if query_video_ids else None)
    if query_video_id:
        return query_video_id

    if len(path_parts) >= 2 and path_parts[0] in {"embed", "live", "shorts", "v"}:
        return normalize_youtube_video_id(path_parts[1])

    return None


def latest_downloaded_video(video_dir: Path, url: str) -> Path | None:
    video_id = youtube_video_id(url)
    media_suffixes = {".mkv", ".mp4", ".webm"}
    files = [
        path
        for path in video_dir.iterdir()
        if path.is_file()
        and path.suffix.lower() in media_suffixes
        and not path.name.endswith(".part")
        and not INTERMEDIATE_FORMAT_RE.search(path.name)
    ]

    if video_id:
        id_matches = [path for path in files if path.stem.endswith(f"[{video_id}]")]
        if id_matches:
            return max(id_matches, key=lambda path: path.stat().st_mtime).resolve()

    return None


def download_video(url: str, video_dir: Path, paths: dict[str, Path], args: argparse.Namespace) -> Path:
    video_dir.mkdir(parents=True, exist_ok=True)

    template = video_dir / "%(title).180B [%(id)s].%(ext)s"
    cmd: list[str | os.PathLike[str]] = [
        paths["python"],
        "-m",
        "yt_dlp",
        "--no-playlist",
        "--windows-filenames",
        "--part",
        "--progress",
        "--progress-delta",
        f"{args.download_progress_delta:g}",
        "-f",
        args.video_format,
        "--merge-output-format",
        args.merge_output_format,
        "--print",
        "after_move:filepath",
        "-o",
        template,
    ]
    if args.force:
        cmd.append("--force-overwrites")
    else:
        cmd.append("--continue")

    if args.cookies_from_browser:
        cmd += ["--cookies-from-browser", args.cookies_from_browser]

    cmd.append(url)
    result = run(cmd, capture_stdout=True, stream_stdout=True, check=False)
    lines = [clean_output_line(line) for line in (result.stdout or "").splitlines() if clean_output_line(line)]
    existing_paths = [Path(line) for line in lines if Path(line).exists()]
    if existing_paths:
        return existing_paths[-1].resolve()

    fallback_path = latest_downloaded_video(video_dir, url)
    if fallback_path and (result.returncode == 0 or not args.force):
        if result.returncode != 0:
            print(f"yt-dlp exited with {result.returncode}, but found final video: {fallback_path}")
        return fallback_path

    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp failed with exit code {result.returncode}")

    raise RuntimeError("could not determine downloaded video path from yt-dlp output")


def audio_codec_args(audio_format: str) -> list[str]:
    if audio_format == "opus":
        return ["-c:a", "libopus", "-b:a", "48k", "-vbr", "on"]
    if audio_format == "m4a":
        return ["-c:a", "aac", "-b:a", "64k"]
    if audio_format == "mp3":
        return ["-c:a", "libmp3lame", "-b:a", "64k"]
    raise ValueError(f"unsupported audio format: {audio_format}")


def extract_audio(video_path: Path, audio_path: Path, audio_format: str, force: bool) -> None:
    if audio_path.exists() and not force:
        return

    audio_path.parent.mkdir(parents=True, exist_ok=True)
    run(
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


def run_whisper(
    audio_path: Path,
    srt_path: Path,
    subs_dir: Path,
    paths: dict[str, Path],
    args: argparse.Namespace,
    *,
    task: str | None = None,
    language: str | None = None,
    model: str | None = None,
) -> None:
    task = task or args.task
    language = args.language if language is None else language
    model = model or args.model
    remove_invalid_whisper_model_cache(paths, model)

    whisper_cmd: list[str | os.PathLike[str]] = [
        paths["whisper"],
        audio_path,
        "--model",
        model,
        "--task",
        task,
        "--output_format",
        "srt",
        "--device",
        args.device,
        "--fp16",
        "True" if args.device == "cuda" else "False",
    ]

    if language and language != "auto":
        whisper_cmd += ["--language", language]

    with tempfile.TemporaryDirectory(prefix="whisper-", dir=subs_dir) as tmp_dir:
        tmp_output_dir = Path(tmp_dir)
        whisper_cmd += ["--output_dir", tmp_output_dir]
        run(whisper_cmd)

        generated_srt = tmp_output_dir / f"{audio_path.stem}.srt"
        if not generated_srt.exists():
            raise RuntimeError(f"Whisper finished, but no .srt file was found at: {generated_srt}")

        if srt_path.exists():
            srt_path.unlink()
        shutil.move(str(generated_srt), str(srt_path))


def parse_srt_timestamp(value: str) -> int:
    hours = int(value[0:2])
    minutes = int(value[3:5])
    seconds = int(value[6:8])
    milliseconds = int(value[9:12])
    return (((hours * 60) + minutes) * 60 + seconds) * 1000 + milliseconds


def format_srt_timestamp(milliseconds: int) -> str:
    milliseconds = max(0, milliseconds)
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"


def normalize_subtitle_text(lines: list[str]) -> str:
    return re.sub(r"\s+", " ", " ".join(line.strip() for line in lines)).strip()


def parse_srt(content: str) -> list[SubtitleCue]:
    normalized = content.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []

    cues: list[SubtitleCue] = []
    for block in re.split(r"\n\s*\n", normalized):
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        if lines[0].isdigit():
            lines = lines[1:]
        if len(lines) < 2:
            continue

        match = SRT_TIME_RE.search(lines[0])
        if not match:
            continue

        text = normalize_subtitle_text(lines[1:])
        if not text:
            continue

        cues.append(
            SubtitleCue(
                start_ms=parse_srt_timestamp(match.group("start")),
                end_ms=parse_srt_timestamp(match.group("end")),
                text=text,
            )
        )

    return cues


def cue_reading_speed(text: str, start_ms: int, end_ms: int) -> float:
    duration_seconds = max((end_ms - start_ms) / 1000, 0.001)
    return len(text) / duration_seconds


def words_in_text(text: str) -> list[str]:
    return WORD_RE.findall(text)


def first_word(text: str) -> str:
    words = words_in_text(text)
    return words[0].casefold() if words else ""


def last_word(text: str) -> str:
    words = words_in_text(text)
    return words[-1].casefold() if words else ""


def terminal_period_is_soft(first: str, second: str, args: argparse.Namespace, *, is_english: bool) -> bool:
    if not should_soften_period_boundaries(args, is_english=is_english):
        return False
    if not TERMINAL_PERIOD_RE.search(first.strip()):
        return False

    first_tail = last_word(first)
    second_head = first_word(second)
    if not first_tail or not second_head:
        return False
    first_words = words_in_text(first)
    if first_tail in FALSE_PERIOD_END_WORDS:
        return True
    if len(first_words) <= 1:
        return False
    if second_head in COORDINATING_SOFT_PERIOD_START_WORDS:
        return False

    return second_head in FALSE_PERIOD_START_WORDS


def should_force_lowercase_after_soft_period(first: str) -> bool:
    return last_word(first) in FALSE_PERIOD_END_WORDS


def remove_terminal_period(text: str) -> str:
    stripped = text.rstrip()
    match = TERMINAL_PERIOD_RE.search(stripped)
    if not match:
        return stripped

    return f"{stripped[: match.start()]}{match.group('trailer')}".strip()


def lowercase_soft_period_continuation(text: str, *, force: bool = False) -> str:
    stripped = text.lstrip()
    leading_space = text[: len(text) - len(stripped)]
    match = re.match(r"(?P<prefix>[\"'(\[]*)(?P<word>[A-Za-z][A-Za-z']*)(?P<rest>.*)", stripped, re.DOTALL)
    if not match:
        return text

    word = match.group("word")
    if word.casefold() not in LOWERCASE_AFTER_SOFT_PERIOD_WORDS and not force:
        return text
    if len(word) > 1 and word.isupper():
        return text
    if force and word.casefold() not in LOWERCASE_AFTER_SOFT_PERIOD_WORDS:
        following_words = words_in_text(match.group("rest"))
        if not following_words or following_words[0][:1].isupper():
            return text

    lowered_word = word[:1].lower() + word[1:]
    return f"{leading_space}{match.group('prefix')}{lowered_word}{match.group('rest')}"


def cue_text_for_merge(
    first: str,
    second: str,
    *,
    soft_period: bool = False,
    force_lowercase: bool = False,
) -> str:
    if soft_period:
        first = remove_terminal_period(first)
        second = lowercase_soft_period_continuation(second, force=force_lowercase)
    return normalize_subtitle_text([first, second])


def may_merge_cues(first: SubtitleCue, second: SubtitleCue, args: argparse.Namespace, *, is_english: bool) -> bool:
    gap_seconds = (second.start_ms - first.end_ms) / 1000
    if gap_seconds > args.compact_gap:
        return False

    soft_period = terminal_period_is_soft(first.text, second.text, args, is_english=is_english)
    if STRONG_PUNCTUATION_RE.search(first.text) and not soft_period:
        return False

    combined_text = cue_text_for_merge(
        first.text,
        second.text,
        soft_period=soft_period,
        force_lowercase=should_force_lowercase_after_soft_period(first.text),
    )
    combined_duration = (second.end_ms - first.start_ms) / 1000
    if combined_duration > args.compact_max_duration:
        return False
    if len(combined_text) > args.compact_max_chars:
        return False
    if cue_reading_speed(combined_text, first.start_ms, second.end_ms) > args.compact_max_cps:
        return False

    return True


def compact_cues(cues: list[SubtitleCue], args: argparse.Namespace, *, is_english: bool = False) -> list[SubtitleCue]:
    if not cues:
        return []

    compacted: list[SubtitleCue] = []
    current = cues[0]

    for next_cue in cues[1:]:
        if may_merge_cues(current, next_cue, args, is_english=is_english):
            soft_period = terminal_period_is_soft(current.text, next_cue.text, args, is_english=is_english)
            current = SubtitleCue(
                start_ms=current.start_ms,
                end_ms=next_cue.end_ms,
                text=cue_text_for_merge(
                    current.text,
                    next_cue.text,
                    soft_period=soft_period,
                    force_lowercase=should_force_lowercase_after_soft_period(current.text),
                ),
            )
        else:
            compacted.append(current)
            current = next_cue

    compacted.append(current)
    return compacted


def wrap_subtitle_text(text: str, line_width: int) -> str:
    width = max(10, line_width)
    return "\n".join(
        textwrap.wrap(
            text,
            width=width,
            break_long_words=False,
            break_on_hyphens=False,
        )
    )


def render_srt(cues: list[SubtitleCue], args: argparse.Namespace) -> str:
    blocks = []
    for index, cue in enumerate(cues, start=1):
        blocks.append(
            "\n".join(
                [
                    str(index),
                    f"{format_srt_timestamp(cue.start_ms)} --> {format_srt_timestamp(cue.end_ms)}",
                    wrap_subtitle_text(cue.text, args.compact_line_width),
                ]
            )
        )
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def openai_source_cues_json(cues: list[SubtitleCue]) -> str:
    payload = [
        {
            "index": index,
            "text": cue.text,
        }
        for index, cue in enumerate(cues, start=1)
    ]
    return json.dumps(payload, ensure_ascii=False, indent=2)


def subtitle_gap_extension_ms(args: argparse.Namespace) -> int:
    seconds = float(getattr(args, "subtitle_gap_extension", DEFAULT_SUBTITLE_GAP_EXTENSION))
    return max(0, int(round(seconds * 1000)))


def extend_subtitle_gaps(
    cues: list[SubtitleCue],
    args: argparse.Namespace,
) -> tuple[list[SubtitleCue], bool]:
    extension_ms = subtitle_gap_extension_ms(args)
    if extension_ms <= 0 or len(cues) < 2:
        return cues, False

    extended: list[SubtitleCue] = []
    changed = False
    for index, cue in enumerate(cues):
        if index == len(cues) - 1:
            extended.append(cue)
            continue

        next_start_ms = cues[index + 1].start_ms
        if cue.end_ms >= next_start_ms:
            extended.append(cue)
            continue

        end_ms = min(cue.end_ms + extension_ms, next_start_ms)
        changed = changed or end_ms != cue.end_ms
        extended.append(SubtitleCue(cue.start_ms, end_ms, cue.text))

    return extended, changed


def strip_env_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def load_env_file(path: str | os.PathLike[str] | None) -> None:
    if not path:
        return

    env_path = Path(path).expanduser()
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        os.environ.setdefault(key, strip_env_quotes(value))


def openai_api_key(args: argparse.Namespace) -> str:
    load_env_file(getattr(args, "openai_env_file", DEFAULT_OPENAI_ENV_FILE))
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Add it to the .env file next to this script "
            "or set it in the environment."
        )
    return api_key


def openai_translation_response_format() -> dict[str, object]:
    return {
        "type": "json_schema",
        "name": "srt_translation",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["translations"],
            "properties": {
                "translations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["index", "text"],
                        "properties": {
                            "index": {"type": "integer"},
                            "text": {"type": "string"},
                        },
                    },
                }
            },
        },
    }


def openai_translation_prompt(
    source_cues_json: str,
    cue_count: int,
    *,
    chunk_label: str | None = None,
    previous_context: str = "",
    next_context: str = "",
) -> str:
    chunk_note = ""
    if chunk_label:
        chunk_note = (
            f"You are translating {chunk_label} from a longer subtitle file. "
            "Return translations only for the SOURCE CUES JSON block in this request.\n"
        )

    context_note = ""
    if previous_context or next_context:
        context_note = "Neighbor context for terminology only; do not translate it:\n"
        if previous_context:
            context_note += f"Before: {previous_context}\n"
        if next_context:
            context_note += f"After: {next_context}\n"
        context_note += "\n"

    return (
        "Translate Dutch subtitle cue texts into natural, concise English.\n"
        f"{chunk_note}"
        "Preserve meaning, names, institutions, speaker intent, and political terms. "
        "Avoid literal Dutch phrasing when natural English is clearer.\n"
        "The source is a JSON array of cue objects. Each source index is the required output index. "
        "For each output item, translate only the text field of the source cue with the same index. "
        "Do not pull text from neighboring cues into the current cue, even when a sentence continues "
        "across cue boundaries. If a source cue is a fragment or starts/ends with ellipses, return a "
        "matching English fragment rather than completing it with adjacent cue text.\n\n"
        f"{context_note}"
        "Return JSON only with this shape: {\"translations\":[{\"index\":1,\"text\":\"...\"}]}.\n"
        f"Translate exactly {cue_count} cues, indexes 1 through {cue_count}. "
        "Do not merge, split, omit, add cues, or output timestamps. Keep text subtitle-length. "
        "Before returning, count the translations array and ensure it contains every requested index.\n\n"
        "SOURCE CUES JSON:\n"
        "```json\n"
        f"{source_cues_json.rstrip()}\n"
        "```"
    )


def openai_translation_repair_prompt(
    source_cues_json: str,
    cue_count: int,
    *,
    chunk_label: str | None,
    validation_errors: list[str],
    previous_context: str = "",
    next_context: str = "",
) -> str:
    chunk_note = ""
    if chunk_label:
        chunk_note = f"This is a repair request for {chunk_label}.\n"

    context_note = ""
    if previous_context or next_context:
        context_note = "Neighbor context for terminology only; do not translate it:\n"
        if previous_context:
            context_note += f"Before: {previous_context}\n"
        if next_context:
            context_note += f"After: {next_context}\n"
        context_note += "\n"

    error_note = ""
    if validation_errors:
        error_note = "The previous response could not be used because:\n"
        for error in validation_errors[:8]:
            error_note += f"- {error}\n"
        error_note += "\n"

    return (
        "Repair an incomplete Dutch-to-English SRT translation.\n"
        f"{chunk_note}"
        f"{error_note}"
        f"{context_note}"
        "Translate only the SOURCE CUES JSON below into natural, concise English.\n"
        "Each source index is local to this repair request and is the required output index. "
        "Translate only the text field of the source cue with the same index. Do not use neighboring "
        "cue text to complete fragments.\n"
        "Return JSON only with this shape: {\"translations\":[{\"index\":1,\"text\":\"...\"}]}.\n"
        f"Return exactly {cue_count} translations, indexes 1 through {cue_count}. "
        "Do not merge, split, omit, add cues, or output timestamps. Keep text subtitle-length. "
        "Before returning, count the translations array and ensure it contains every requested index.\n\n"
        "SOURCE CUES JSON:\n"
        "```json\n"
        f"{source_cues_json.rstrip()}\n"
        "```"
    )


def cue_context_text(cues: list[SubtitleCue], max_chars: int = 1200) -> str:
    text = " ".join(cue.text for cue in cues)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_chars:
        return text
    return textwrap.shorten(text, width=max_chars, placeholder=" ...")


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def openai_translation_checkpoint_path(english_srt_path: Path) -> Path:
    return english_srt_path.with_name(f"{english_srt_path.stem}.partial.json")


def openai_translation_metadata(
    source_srt: str,
    source_cues: list[SubtitleCue],
    args: argparse.Namespace,
    chunk_size: int,
) -> dict[str, object]:
    return {
        "schema": 2,
        "source_format": "cue_json_v1",
        "source_sha256": text_sha256(source_srt),
        "cue_count": len(source_cues),
        "model": getattr(args, "openai_translation_model", DEFAULT_OPENAI_TRANSLATION_MODEL),
        "reasoning_effort": getattr(args, "openai_reasoning_effort", DEFAULT_OPENAI_TRANSLATION_REASONING),
        "chunk_cues": chunk_size,
        "context_cues": int(
            getattr(args, "openai_translation_context_cues", DEFAULT_OPENAI_TRANSLATION_CONTEXT_CUES)
        ),
    }


def load_openai_translation_checkpoint(
    checkpoint_path: Path,
    metadata: dict[str, object],
) -> list[str | None] | None:
    if not checkpoint_path.exists():
        return None
    try:
        data = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("metadata") != metadata:
        return None
    translations = data.get("translations")
    cue_count = metadata.get("cue_count")
    if not isinstance(cue_count, int) or not isinstance(translations, list):
        return None
    if len(translations) != cue_count:
        return None
    if not all(item is None or isinstance(item, str) for item in translations):
        return None
    return translations


def save_openai_translation_checkpoint(
    checkpoint_path: Path,
    metadata: dict[str, object],
    translations: list[str | None],
) -> None:
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = checkpoint_path.with_suffix(f"{checkpoint_path.suffix}.tmp")
    payload = {
        "metadata": metadata,
        "translations": translations,
    }
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    temp_path.replace(checkpoint_path)


def openai_translation_chunk_size(args: argparse.Namespace, cue_count: int) -> int:
    requested = int(getattr(args, "openai_translation_chunk_cues", DEFAULT_OPENAI_TRANSLATION_CHUNK_CUES))
    if requested <= 0:
        return cue_count
    return max(1, requested)


def openai_translation_payload(args: argparse.Namespace, prompt: str) -> dict[str, object]:
    return {
        "model": getattr(args, "openai_translation_model", DEFAULT_OPENAI_TRANSLATION_MODEL),
        "input": prompt,
        "reasoning": {"effort": getattr(args, "openai_reasoning_effort", DEFAULT_OPENAI_TRANSLATION_REASONING)},
        "text": {"format": openai_translation_response_format()},
        "store": False,
    }


def format_index_list(indexes: list[int], *, limit: int = 12) -> str:
    if len(indexes) <= limit:
        return ", ".join(str(index) for index in indexes)
    shown = ", ".join(str(index) for index in indexes[:limit])
    return f"{shown}, ... ({len(indexes)} total)"


def repair_cue_chunk_translations_with_openai(
    source_cues: list[SubtitleCue],
    parse_result: OpenAITranslationParseResult,
    args: argparse.Namespace,
    *,
    chunk_label: str | None = None,
    previous_context: str = "",
    next_context: str = "",
) -> list[str]:
    missing_indexes = parse_result.missing_indexes
    if not missing_indexes:
        return [text for text in parse_result.texts if text is not None]

    label = f" for {chunk_label}" if chunk_label else ""
    print(
        f"OpenAI returned an incomplete translation{label}; "
        f"requesting repair for cue(s) {format_index_list(missing_indexes)}."
    )

    missing_cues = [source_cues[index - 1] for index in missing_indexes]
    repair_source_cues = openai_source_cues_json(missing_cues)
    repair_label = f"{chunk_label} repair" if chunk_label else "repair"
    payload = openai_translation_payload(
        args,
        openai_translation_repair_prompt(
            repair_source_cues,
            len(missing_cues),
            chunk_label=chunk_label,
            validation_errors=parse_result.errors,
            previous_context=previous_context,
            next_context=next_context,
        ),
    )
    response = openai_responses_api_request(args, payload)
    print_openai_usage(response, chunk_label=repair_label)
    repaired_texts = parse_openai_translations(response_output_text(response), len(missing_cues))

    texts = list(parse_result.texts)
    for missing_index, repaired_text in zip(missing_indexes, repaired_texts):
        texts[missing_index - 1] = repaired_text

    still_missing = [index + 1 for index, text in enumerate(texts) if text is None]
    if still_missing:
        raise RuntimeError(
            "OpenAI translation repair did not fill cue(s): "
            f"{format_index_list(still_missing)}"
        )

    return [text for text in texts if text is not None]


def translate_cue_chunk_with_openai(
    source_cues: list[SubtitleCue],
    args: argparse.Namespace,
    *,
    chunk_label: str | None = None,
    previous_context: str = "",
    next_context: str = "",
) -> list[str]:
    source_cues_json = openai_source_cues_json(source_cues)
    payload = openai_translation_payload(
        args,
        openai_translation_prompt(
            source_cues_json,
            len(source_cues),
            chunk_label=chunk_label,
            previous_context=previous_context,
            next_context=next_context,
        ),
    )
    response = openai_responses_api_request(args, payload)
    print_openai_usage(response, chunk_label=chunk_label)
    parse_result = collect_openai_translations(response_output_text(response), len(source_cues))
    if parse_result.complete:
        return [text for text in parse_result.texts if text is not None]
    return repair_cue_chunk_translations_with_openai(
        source_cues,
        parse_result,
        args,
        chunk_label=chunk_label,
        previous_context=previous_context,
        next_context=next_context,
    )


def print_openai_usage(data: dict[str, object], *, chunk_label: str | None = None) -> None:
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return

    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    total_tokens = usage.get("total_tokens")
    output_details = usage.get("output_tokens_details")
    reasoning_tokens = None
    if isinstance(output_details, dict):
        reasoning_tokens = output_details.get("reasoning_tokens")

    parts: list[str] = []
    if isinstance(input_tokens, int):
        parts.append(f"input={input_tokens}")
    if isinstance(output_tokens, int):
        parts.append(f"output={output_tokens}")
    if isinstance(reasoning_tokens, int):
        parts.append(f"reasoning={reasoning_tokens}")
    if isinstance(total_tokens, int):
        parts.append(f"total={total_tokens}")
    if not parts:
        return

    label = f" for {chunk_label}" if chunk_label else ""
    print(f"OpenAI token usage{label}: " + ", ".join(parts))


def translate_cues_with_openai(
    source_srt: str,
    source_cues: list[SubtitleCue],
    english_srt_path: Path,
    args: argparse.Namespace,
) -> list[str]:
    chunk_size = openai_translation_chunk_size(args, len(source_cues))
    if chunk_size >= len(source_cues):
        return translate_cue_chunk_with_openai(source_cues, args)

    checkpoint_path = openai_translation_checkpoint_path(english_srt_path)
    metadata = openai_translation_metadata(source_srt, source_cues, args, chunk_size)
    context_cues = int(getattr(args, "openai_translation_context_cues", DEFAULT_OPENAI_TRANSLATION_CONTEXT_CUES))
    translated_texts = load_openai_translation_checkpoint(checkpoint_path, metadata)
    if translated_texts is None:
        translated_texts = [None] * len(source_cues)

    chunks = [
        (start, min(start + chunk_size, len(source_cues)))
        for start in range(0, len(source_cues), chunk_size)
    ]
    print(
        f"OpenAI translation will use {len(chunks)} chunks "
        f"of up to {chunk_size} cues; checkpoint: {checkpoint_path}"
    )

    for chunk_number, (start, end) in enumerate(chunks, start=1):
        if all(translated_texts[start:end]):
            print(f"Reusing completed OpenAI translation chunk {chunk_number}/{len(chunks)}.")
            continue

        previous_start = max(0, start - context_cues)
        next_end = min(len(source_cues), end + context_cues)
        chunk_label = f"chunk {chunk_number}/{len(chunks)} (global cues {start + 1}-{end})"
        print(f"Translating OpenAI subtitle {chunk_label}...")
        chunk_translations = translate_cue_chunk_with_openai(
            source_cues[start:end],
            args,
            chunk_label=chunk_label,
            previous_context=cue_context_text(source_cues[previous_start:start]),
            next_context=cue_context_text(source_cues[end:next_end]),
        )
        translated_texts[start:end] = chunk_translations
        save_openai_translation_checkpoint(checkpoint_path, metadata, translated_texts)

    missing_indexes = [index + 1 for index, text in enumerate(translated_texts) if text is None]
    if missing_indexes:
        raise RuntimeError(f"OpenAI translation checkpoint is incomplete at cues: {missing_indexes[:10]}")

    return [text for text in translated_texts if text is not None]


def retry_after_seconds(exc: urlerror.HTTPError) -> float | None:
    retry_after = exc.headers.get("Retry-After")
    if not retry_after:
        return None
    try:
        seconds = float(retry_after)
    except ValueError:
        return None
    if seconds < 0:
        return None
    return seconds


def openai_retry_delay(attempt: int, retry_after: float | None = None) -> float:
    if retry_after is not None:
        return retry_after
    return DEFAULT_OPENAI_RETRY_INITIAL_DELAY * (2 ** max(0, attempt - 1))


def openai_responses_api_request(args: argparse.Namespace, payload: dict[str, object]) -> dict[str, object]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    max_retries = max(0, int(getattr(args, "openai_max_retries", DEFAULT_OPENAI_MAX_RETRIES)))
    total_attempts = max_retries + 1

    for attempt in range(1, total_attempts + 1):
        request = urlrequest.Request(
            "https://api.openai.com/v1/responses",
            data=body,
            headers={
                "Authorization": f"Bearer {openai_api_key(args)}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urlrequest.urlopen(
                request,
                timeout=getattr(args, "openai_timeout", DEFAULT_OPENAI_TIMEOUT),
            ) as response:
                return json.loads(response.read().decode("utf-8"))
        except urlerror.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace").strip()
            details = details[:2000] if details else exc.reason
            if exc.code not in TRANSIENT_OPENAI_HTTP_STATUS_CODES or attempt >= total_attempts:
                raise RuntimeError(f"OpenAI request failed with HTTP {exc.code}: {details}") from exc
            delay = openai_retry_delay(attempt, retry_after_seconds(exc))
            print(
                f"OpenAI request failed with HTTP {exc.code}; retrying "
                f"{attempt}/{max_retries} in {delay:g}s..."
            )
            time.sleep(delay)
        except TRANSIENT_OPENAI_NETWORK_ERRORS as exc:
            if attempt >= total_attempts:
                reason = getattr(exc, "reason", exc)
                raise RuntimeError(f"OpenAI request failed: {reason}") from exc
            delay = openai_retry_delay(attempt)
            reason = getattr(exc, "reason", exc)
            print(f"OpenAI request failed: {reason}; retrying {attempt}/{max_retries} in {delay:g}s...")
            time.sleep(delay)

    raise RuntimeError("OpenAI request failed after retries.")


def response_output_text(data: dict[str, object]) -> str:
    output_text = data.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text

    parts: list[str] = []
    output = data.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for entry in content:
                if not isinstance(entry, dict):
                    continue
                text = entry.get("text")
                if isinstance(text, str):
                    parts.append(text)

    text = "".join(parts).strip()
    if not text:
        status = data.get("status")
        raise RuntimeError(f"OpenAI response did not include output text; status={status!r}")
    return text


def strip_json_code_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped

    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def collect_openai_translations(output_text: str, expected_count: int) -> OpenAITranslationParseResult:
    try:
        payload = json.loads(strip_json_code_fence(output_text))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"OpenAI returned invalid translation JSON: {exc}") from exc

    translations = payload.get("translations") if isinstance(payload, dict) else None
    if not isinstance(translations, list):
        raise RuntimeError("OpenAI translation JSON is missing a translations list.")

    texts: list[str | None] = [None] * expected_count
    errors: list[str] = []
    if len(translations) != expected_count:
        errors.append(
            f"OpenAI returned {len(translations)} translations for {expected_count} subtitle cues."
        )

    for response_index, item in enumerate(translations, start=1):
        if not isinstance(item, dict):
            errors.append(f"OpenAI translation response item #{response_index} is not an object.")
            continue
        index = item.get("index")
        text = item.get("text")
        if not isinstance(index, int) or isinstance(index, bool):
            errors.append(f"OpenAI translation response item #{response_index} has non-integer index {index!r}.")
            continue
        if index < 1 or index > expected_count:
            errors.append(
                f"OpenAI translation response item #{response_index} has out-of-range index {index!r}."
            )
            continue
        if not isinstance(text, str):
            errors.append(f"OpenAI translation #{index} text is not a string.")
            continue
        text = normalize_subtitle_text(text.splitlines())
        if not text:
            errors.append(f"OpenAI translation #{index} is empty.")
            continue
        if texts[index - 1] is not None:
            errors.append(f"OpenAI returned duplicate translation index {index}.")
            continue
        texts[index - 1] = text

    return OpenAITranslationParseResult(texts=texts, errors=errors)


def parse_openai_translations(output_text: str, expected_count: int) -> list[str]:
    result = collect_openai_translations(output_text, expected_count)
    if not result.complete:
        message = "; ".join(result.errors) if result.errors else "OpenAI returned incomplete translations."
        raise RuntimeError(
            f"{message} Missing cue(s): {format_index_list(result.missing_indexes)}."
        )
    texts = [text for text in result.texts if text is not None]
    if len(texts) != expected_count:
        raise RuntimeError(f"OpenAI returned {len(texts)} usable translations for {expected_count} subtitle cues.")
    return texts


def translate_srt_with_openai(primary_srt_path: Path, english_srt_path: Path, args: argparse.Namespace) -> None:
    source_srt = primary_srt_path.read_text(encoding="utf-8-sig")
    source_cues = parse_srt(source_srt)
    if not source_cues:
        raise RuntimeError(f"no subtitle cues found in primary SRT: {primary_srt_path}")

    translated_texts = translate_cues_with_openai(source_srt, source_cues, english_srt_path, args)
    translated_cues = [
        SubtitleCue(cue.start_ms, cue.end_ms, translated_text)
        for cue, translated_text in zip(source_cues, translated_texts)
    ]

    english_srt_path.parent.mkdir(parents=True, exist_ok=True)
    english_srt_path.write_text(render_srt(translated_cues, args), encoding="utf-8", newline="\n")
    checkpoint_path = openai_translation_checkpoint_path(english_srt_path)
    if checkpoint_path.exists():
        checkpoint_path.unlink()


def compact_srt_content(content: str, args: argparse.Namespace, *, is_english: bool = False) -> str:
    return render_srt(compact_cues(parse_srt(content), args, is_english=is_english), args)


def extend_subtitle_gaps_srt_content(content: str, args: argparse.Namespace) -> tuple[str, bool]:
    cues, changed = extend_subtitle_gaps(parse_srt(content), args)
    if not cues:
        return "", False
    return render_srt(cues, args), changed


def uncompacted_backup_path(path: Path) -> Path:
    return path.with_name(f"{path.stem}.uncompact{path.suffix}")


def save_uncompacted_backup(path: Path, content: str, *, label: str) -> Path | None:
    backup_path = uncompacted_backup_path(path)
    if backup_path.exists():
        return None

    backup_path.write_text(content, encoding="utf-8", newline="\n")
    print(f"Saved {label} uncompacted subtitle backup: {backup_path}")
    return backup_path


def restore_subtitle_from_uncompacted_backup(
    path: Path,
    args: argparse.Namespace,
    *,
    is_english: bool,
    label: str,
) -> bool:
    if path.exists():
        return False

    backup_path = uncompacted_backup_path(path)
    if not backup_path.exists():
        return False

    backup_content = backup_path.read_text(encoding="utf-8-sig")
    if should_compact_subtitles(args, is_english=is_english):
        restored_content = compact_srt_content(backup_content, args, is_english=is_english)
        action = "Rebuilt compacted"
    else:
        restored_content = backup_content.replace("\r\n", "\n").replace("\r", "\n")
        action = "Restored"

    if not restored_content:
        return False

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(restored_content, encoding="utf-8", newline="\n")
    print(f"{action} {label} subtitles from uncompacted backup: {path}")
    return True


def compact_srt_file(path: Path, args: argparse.Namespace, *, is_english: bool, label: str) -> bool:
    if not path.exists():
        return False

    original = path.read_text(encoding="utf-8-sig")
    compacted = compact_srt_content(original, args, is_english=is_english)
    if not compacted:
        return False

    if original.replace("\r\n", "\n") == compacted:
        return False

    save_uncompacted_backup(path, original, label=label)
    path.write_text(compacted, encoding="utf-8", newline="\n")
    print(f"Compacted {label} subtitles: {path}")
    return True


def extend_subtitle_gaps_file(path: Path, args: argparse.Namespace, *, label: str) -> bool:
    if not path.exists():
        return False

    original = path.read_text(encoding="utf-8-sig")
    extended, changed = extend_subtitle_gaps_srt_content(original, args)
    if not changed or not extended:
        return False

    path.write_text(extended, encoding="utf-8", newline="\n")
    print(f"Extended {label} subtitle gaps: {path}")
    return True


def align_subtitle_timings_to_reference_content(
    reference_content: str,
    target_content: str,
    args: argparse.Namespace,
) -> tuple[str, bool]:
    reference_cues = parse_srt(reference_content)
    target_cues = parse_srt(target_content)
    if not reference_cues or len(reference_cues) != len(target_cues):
        return "", False

    changed = False
    aligned_cues: list[SubtitleCue] = []
    for reference_cue, target_cue in zip(reference_cues, target_cues):
        changed = changed or (
            reference_cue.start_ms != target_cue.start_ms
            or reference_cue.end_ms != target_cue.end_ms
        )
        aligned_cues.append(
            SubtitleCue(
                reference_cue.start_ms,
                reference_cue.end_ms,
                target_cue.text,
            )
        )

    if not changed:
        return "", False
    return render_srt(aligned_cues, args), True


def align_subtitle_timings_to_reference_file(
    reference_srt_path: Path,
    target_srt_path: Path,
    args: argparse.Namespace,
    *,
    label: str,
) -> bool:
    if not reference_srt_path.exists() or not target_srt_path.exists():
        return False

    reference = reference_srt_path.read_text(encoding="utf-8-sig")
    target = target_srt_path.read_text(encoding="utf-8-sig")
    aligned, changed = align_subtitle_timings_to_reference_content(reference, target, args)
    if not changed or not aligned:
        return False

    target_srt_path.write_text(aligned, encoding="utf-8", newline="\n")
    print(f"Aligned {label} subtitle timings to primary subtitles: {target_srt_path}")
    return True


def ensure_compacted_subtitle_pair(
    sidecar_srt_path: Path,
    archive_srt_path: Path,
    args: argparse.Namespace,
    *,
    is_english: bool,
    label: str,
    force: bool,
) -> bool:
    if force or not should_compact_subtitles(args, is_english=is_english):
        return False

    if sidecar_srt_path.exists():
        sidecar_changed = compact_srt_file(
            sidecar_srt_path,
            args,
            is_english=is_english,
            label=f"{label} sidecar",
        )
    else:
        sidecar_changed = False

    if archive_srt_path.exists():
        archive_changed = compact_srt_file(
            archive_srt_path,
            args,
            is_english=is_english,
            label=f"{label} archive",
        )
    else:
        archive_changed = False

    changed = sidecar_changed or archive_changed

    if sidecar_srt_path.exists():
        sync_subtitle_archive(sidecar_srt_path, archive_srt_path)
    elif archive_srt_path.exists():
        seed_sidecar_from_archive(sidecar_srt_path, archive_srt_path)

    return changed


def ensure_matching_subtitle_timing_pair(
    reference_srt_path: Path,
    target_sidecar_srt_path: Path,
    target_archive_srt_path: Path,
    args: argparse.Namespace,
    *,
    label: str,
    force: bool,
) -> bool:
    if force:
        return False

    changed = align_subtitle_timings_to_reference_file(
        reference_srt_path,
        target_sidecar_srt_path,
        args,
        label=f"{label} sidecar",
    )

    if target_sidecar_srt_path.exists():
        sync_subtitle_archive(target_sidecar_srt_path, target_archive_srt_path)
    elif target_archive_srt_path.exists():
        archive_changed = align_subtitle_timings_to_reference_file(
            reference_srt_path,
            target_archive_srt_path,
            args,
            label=f"{label} archive",
        )
        changed = changed or archive_changed
        seed_sidecar_from_archive(target_sidecar_srt_path, target_archive_srt_path)

    return changed


def ensure_extended_subtitle_gap_pair(
    sidecar_srt_path: Path,
    archive_srt_path: Path,
    args: argparse.Namespace,
    *,
    label: str,
    force: bool,
) -> bool:
    if force or subtitle_gap_extension_ms(args) <= 0:
        return False

    sidecar_changed = extend_subtitle_gaps_file(
        sidecar_srt_path,
        args,
        label=f"{label} sidecar",
    )
    archive_changed = extend_subtitle_gaps_file(
        archive_srt_path,
        args,
        label=f"{label} archive",
    )
    changed = sidecar_changed or archive_changed

    if sidecar_srt_path.exists():
        sync_subtitle_archive(sidecar_srt_path, archive_srt_path)
    elif archive_srt_path.exists():
        seed_sidecar_from_archive(sidecar_srt_path, archive_srt_path)

    return changed


def finalize_subtitle_pair(
    sidecar_srt_path: Path,
    archive_srt_path: Path,
    args: argparse.Namespace,
    *,
    is_english: bool,
    label: str,
) -> None:
    if should_compact_subtitles(args, is_english=is_english):
        ensure_compacted_subtitle_pair(
            sidecar_srt_path,
            archive_srt_path,
            args,
            is_english=is_english,
            label=label,
            force=False,
        )
    else:
        sync_subtitle_archive(sidecar_srt_path, archive_srt_path)

    ensure_extended_subtitle_gap_pair(
        sidecar_srt_path,
        archive_srt_path,
        args,
        label=label,
        force=False,
    )


def whisper_cache_root() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "whisper"


def whisper_model_url(paths: dict[str, Path], model: str) -> str | None:
    result = subprocess.run(
        [
            str(paths["python"]),
            "-c",
            "import sys, whisper; print(whisper._MODELS[sys.argv[1]])",
            model,
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def remove_invalid_whisper_model_cache(paths: dict[str, Path], model: str) -> None:
    url = whisper_model_url(paths, model)
    if not url:
        return

    url_parts = url.rstrip("/").split("/")
    if len(url_parts) < 2:
        return

    expected_sha256 = url_parts[-2].casefold()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        return

    cache_path = whisper_cache_root() / url_parts[-1]
    if not cache_path.exists():
        return

    if cache_path.stat().st_size == 0:
        cache_path.unlink()
        print(f"Removed empty Whisper model cache file: {cache_path}")
        return

    actual_sha256 = sha256_file(cache_path).casefold()
    if actual_sha256 != expected_sha256:
        cache_path.unlink()
        print(f"Removed corrupt Whisper model cache file: {cache_path}")


def seed_sidecar_from_archive(sidecar_srt_path: Path, archive_srt_path: Path) -> bool:
    if sidecar_srt_path.exists() or not archive_srt_path.exists():
        return False

    sidecar_srt_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(archive_srt_path, sidecar_srt_path)
    return True


def sync_subtitle_archive(sidecar_srt_path: Path, archive_srt_path: Path) -> None:
    if not sidecar_srt_path.exists() or sidecar_srt_path.resolve() == archive_srt_path.resolve():
        return

    archive_srt_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(sidecar_srt_path, archive_srt_path)


def hydrate_subtitle_pair(
    label: str,
    sidecar_srt_path: Path,
    archive_srt_path: Path,
    args: argparse.Namespace,
    *,
    is_english: bool,
    force: bool,
) -> None:
    if force:
        return

    restore_subtitle_from_uncompacted_backup(
        sidecar_srt_path,
        args,
        is_english=is_english,
        label=f"{label} sidecar",
    )
    restore_subtitle_from_uncompacted_backup(
        archive_srt_path,
        args,
        is_english=is_english,
        label=f"{label} archive",
    )

    if seed_sidecar_from_archive(sidecar_srt_path, archive_srt_path):
        print()
        print(f"Copied existing {label} subtitle archive next to the video for mpv auto-detection.")
    elif sidecar_srt_path.exists() and not archive_srt_path.exists():
        sync_subtitle_archive(sidecar_srt_path, archive_srt_path)
        print()
        print(f"Copied existing {label} subtitle sidecar into the subtitle archive.")


def subtitle_pair_ready(sidecar_srt_path: Path, archive_srt_path: Path) -> bool:
    return sidecar_srt_path.exists() and archive_srt_path.exists()


def dual_sub_primary_font_size(args: argparse.Namespace) -> float:
    if args.dual_sub_primary_font_size is not None:
        return args.dual_sub_primary_font_size
    return args.dual_sub_font_size * DEFAULT_DUAL_SUB_PRIMARY_FONT_SCALE


def dual_sub_secondary_font_size(args: argparse.Namespace) -> float:
    if args.dual_sub_secondary_font_size is not None:
        return args.dual_sub_secondary_font_size
    return args.dual_sub_font_size


def parse_css_color(value: str) -> tuple[int, int, int, int]:
    hex_value = value.strip()
    if hex_value.startswith("#"):
        hex_value = hex_value[1:]

    if len(hex_value) == 6:
        red, green, blue = (int(hex_value[index : index + 2], 16) for index in (0, 2, 4))
        alpha = 255
    elif len(hex_value) == 8:
        red, green, blue, alpha = (int(hex_value[index : index + 2], 16) for index in (0, 2, 4, 6))
    else:
        raise ValueError

    return red, green, blue, alpha


def css_color_to_ass_color(value: str) -> str:
    red, green, blue, css_alpha = parse_css_color(value)
    ass_alpha = 255 - css_alpha
    return f"&H{ass_alpha:02X}{blue:02X}{green:02X}{red:02X}"


def css_color_to_mpv_color(value: str) -> str:
    red, green, blue, alpha = parse_css_color(value)
    return f"#{alpha:02X}{red:02X}{green:02X}{blue:02X}"


def mpv_subtitle_color(value: str) -> str:
    try:
        return css_color_to_mpv_color(value)
    except ValueError as exc:
        raise RuntimeError(f"invalid subtitle color '{value}'; use #RRGGBB or #RRGGBBAA") from exc


def ass_timestamp(milliseconds: int) -> str:
    milliseconds = max(0, milliseconds)
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    centiseconds = milliseconds // 10
    return f"{hours}:{minutes:02}:{seconds:02}.{centiseconds:02}"


def ass_escape_text(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace("{", "\\{")
        .replace("}", "\\}")
        .replace("\n", r"\N")
    )


def ass_alignment_from_sub_pos(position: float) -> int:
    if position <= 33:
        return 8
    if position >= 67:
        return 2
    return 5


def ass_margin_v_from_sub_pos(position: float) -> int:
    if position <= 33:
        return max(30, round(1080 * max(position, 0) / 100))
    if position >= 67:
        return max(35, round(1080 * max(100 - min(position, 100), 0) / 100))
    return 0


def write_ass_subtitle(
    srt_path: Path,
    ass_path: Path,
    *,
    color: str,
    position: float,
    font_size: float,
) -> None:
    try:
        primary_color = css_color_to_ass_color(color)
    except ValueError as exc:
        raise RuntimeError(f"invalid subtitle color '{color}'; use #RRGGBB or #RRGGBBAA") from exc

    cues = parse_srt(srt_path.read_text(encoding="utf-8-sig"))
    alignment = ass_alignment_from_sub_pos(position)
    margin_v = ass_margin_v_from_sub_pos(position)
    ass_path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "PlayResX: 1920",
        "PlayResY: 1080",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        (
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
            "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
            "MarginL, MarginR, MarginV, Encoding"
        ),
        (
            f"Style: Default,Segoe UI,{font_size:g},{primary_color},"
            "&H00FFFFFF,&H00000000,&H96000000,0,0,0,0,100,100,0,0,1,3,1,"
            f"{alignment},40,40,{margin_v},1"
        ),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    for cue in cues:
        lines.append(
            "Dialogue: "
            f"0,{ass_timestamp(cue.start_ms)},{ass_timestamp(cue.end_ms)},"
            f"Default,,0,0,0,,{ass_escape_text(cue.text)}"
        )

    ass_path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def dual_subtitle_playback_paths(srt_paths: list[Path], args: argparse.Namespace, temp_dir: Path) -> list[Path]:
    secondary_ass = temp_dir / f"{srt_paths[1].stem}.secondary.ass"

    write_ass_subtitle(
        srt_paths[1],
        secondary_ass,
        color=args.dual_sub_secondary_color,
        position=args.dual_sub_secondary_pos,
        font_size=dual_sub_secondary_font_size(args),
    )

    return [srt_paths[0], secondary_ass, *srt_paths[2:]]


def play_video(video_path: Path, srt_paths: list[Path], args: argparse.Namespace) -> None:
    cmd: list[str | os.PathLike[str]] = ["mpv", "--sub-auto=no"]
    existing_srt_paths = [srt_path for srt_path in srt_paths if srt_path.exists()]

    temp_dir_context = None
    try:
        if args.dual_subs and len(existing_srt_paths) >= 2:
            temp_dir_context = tempfile.TemporaryDirectory(prefix="yt-whisper-subs-ass-")
            temp_dir = Path(temp_dir_context.__enter__())
            subtitle_paths = dual_subtitle_playback_paths(existing_srt_paths, args, temp_dir)
        else:
            subtitle_paths = existing_srt_paths

        for subtitle_path in subtitle_paths:
            cmd.append(f"--sub-file={subtitle_path}")

        if args.dual_subs and len(existing_srt_paths) >= 2:
            cmd += [
                "--sid=1",
                "--secondary-sid=2",
                f"--sub-color={mpv_subtitle_color(args.dual_sub_primary_color)}",
                f"--sub-font-size={dual_sub_primary_font_size(args):g}",
                f"--sub-pos={args.dual_sub_primary_pos:g}",
                "--secondary-sub-ass-override=no",
            ]

        cmd.append(video_path)
        run(cmd)
    finally:
        if temp_dir_context is not None:
            temp_dir_context.__exit__(None, None, None)


def resolve_output_dir(out_dir: str | None) -> Path:
    if out_dir:
        return Path(out_dir).expanduser().resolve()
    return DEFAULT_OUTPUT_DIR.resolve()


def print_yield_paths(
    video_path: Path,
    audio_path: Path,
    sidecar_srt_path: Path,
    archive_srt_path: Path,
    *,
    make_english_subs: bool,
    english_sidecar_srt_path: Path,
    english_archive_srt_path: Path,
) -> None:
    print()
    print(f"Video: {video_path}")
    print(f"Audio: {audio_path}")
    print(f"SRT:   {sidecar_srt_path}")
    print(f"Archive SRT: {archive_srt_path}")
    if make_english_subs:
        print(f"English SRT: {english_sidecar_srt_path}")
        print(f"English Archive SRT: {english_archive_srt_path}")


def print_done(
    video_path: Path,
    audio_path: Path,
    sidecar_srt_path: Path,
    archive_srt_path: Path,
    *,
    make_english_subs: bool,
    english_sidecar_srt_path: Path,
    english_archive_srt_path: Path,
) -> None:
    print()
    print("Done.")
    print(f"Video: {video_path}")
    print(f"Audio: {audio_path if audio_path.exists() else '(deleted)'}")
    print(f"Subs:  {sidecar_srt_path}")
    print(f"Archive Subs: {archive_srt_path}")
    if make_english_subs:
        print(f"English Subs: {english_sidecar_srt_path}")
        print(f"English Archive Subs: {english_archive_srt_path}")


def main() -> int:
    configure_stdio()
    args = parse_args()
    paths = venv_paths()

    try:
        if args.install_tools:
            install_tools()

        if not args.no_play:
            require_command("mpv")

        out_dir = resolve_output_dir(args.out_dir)
        video_dir = out_dir / "videos"
        audio_dir = out_dir / "audio"
        subs_dir = out_dir / "subtitles"
        video_dir.mkdir(parents=True, exist_ok=True)
        audio_dir.mkdir(parents=True, exist_ok=True)
        subs_dir.mkdir(parents=True, exist_ok=True)
        python_deps_ready = False

        if args.url:
            video_path = None if args.force else latest_downloaded_video(video_dir, args.url)
            if video_path:
                print()
                print(f"Found existing video yield: {video_path}")
            else:
                ensure_python_deps(paths, args)
                python_deps_ready = True
                require_command("ffmpeg")
                print()
                print("Downloading compressed lossy video stream...")
                video_path = download_video(args.url, video_dir, paths, args)
        else:
            video_path = resolve_video_path(args.video_file)

        video_base = video_path.stem
        audio_path = audio_dir / f"{video_base}.{args.audio_format}"
        sidecar_srt_path = video_path.with_suffix(".srt")
        archive_srt_path = subs_dir / f"{video_base}.srt"
        make_english_subs = args.english_for_dutch and args.task == "transcribe" and is_dutch_language(args.language)
        args.compact_primary_for_openai_translation = make_english_subs and uses_openai_english_translation(args)
        english_sidecar_srt_path = video_path.with_name(f"{video_base}.en.srt")
        english_archive_srt_path = subs_dir / f"{video_base}.en.srt"

        hydrate_subtitle_pair(
            "primary",
            sidecar_srt_path,
            archive_srt_path,
            args,
            is_english=False,
            force=args.force,
        )
        if make_english_subs:
            hydrate_subtitle_pair(
                "English",
                english_sidecar_srt_path,
                english_archive_srt_path,
                args,
                is_english=True,
                force=args.force,
            )

        ensure_compacted_subtitle_pair(
            sidecar_srt_path,
            archive_srt_path,
            args,
            is_english=False,
            label="primary",
            force=args.force,
        )
        ensure_extended_subtitle_gap_pair(
            sidecar_srt_path,
            archive_srt_path,
            args,
            label="primary",
            force=args.force,
        )
        if make_english_subs:
            ensure_compacted_subtitle_pair(
                english_sidecar_srt_path,
                english_archive_srt_path,
                args,
                is_english=True,
                label="English",
                force=args.force,
            )
            ensure_extended_subtitle_gap_pair(
                english_sidecar_srt_path,
                english_archive_srt_path,
                args,
                label="English",
                force=args.force,
            )
            if uses_openai_english_translation(args):
                ensure_matching_subtitle_timing_pair(
                    sidecar_srt_path,
                    english_sidecar_srt_path,
                    english_archive_srt_path,
                    args,
                    label="English",
                    force=args.force,
                )

        print_yield_paths(
            video_path,
            audio_path,
            sidecar_srt_path,
            archive_srt_path,
            make_english_subs=make_english_subs,
            english_sidecar_srt_path=english_sidecar_srt_path,
            english_archive_srt_path=english_archive_srt_path,
        )

        primary_ready = subtitle_pair_ready(sidecar_srt_path, archive_srt_path)
        english_ready = (not make_english_subs) or subtitle_pair_ready(
            english_sidecar_srt_path,
            english_archive_srt_path,
        )
        all_yields_ready = video_path.exists() and primary_ready and english_ready

        if all_yields_ready and not args.force:
            if args.install_python_deps and not python_deps_ready:
                ensure_python_deps(paths, args)
                python_deps_ready = True

            print()
            print("All requested yields are already present; skipping yt-dlp, ffmpeg, CUDA, Whisper, and OpenAI.")

            if not args.keep_audio and audio_path.exists():
                audio_path.unlink()

            if args.no_play:
                print_done(
                    video_path,
                    audio_path,
                    sidecar_srt_path,
                    archive_srt_path,
                    make_english_subs=make_english_subs,
                    english_sidecar_srt_path=english_sidecar_srt_path,
                    english_archive_srt_path=english_archive_srt_path,
                )
            else:
                print()
                print("Opening in mpv with subtitles...")
                srt_paths = [sidecar_srt_path]
                if make_english_subs:
                    srt_paths.append(english_sidecar_srt_path)
                play_video(video_path, srt_paths, args)

            return 0

        need_primary_generation = (not primary_ready) or args.force
        need_english_generation = make_english_subs and (
            not subtitle_pair_ready(english_sidecar_srt_path, english_archive_srt_path) or args.force
        )
        need_whisper = need_primary_generation or (
            need_english_generation and not uses_openai_english_translation(args)
        )

        if args.install_python_deps and not python_deps_ready:
            ensure_python_deps(paths, args)
            python_deps_ready = True

        if need_whisper:
            if not python_deps_ready:
                ensure_python_deps(paths, args)
                python_deps_ready = True
            require_command("ffmpeg")

            print()
            print("Checking PyTorch CUDA visibility...")
            if args.device == "cuda" and not check_cuda(paths):
                raise RuntimeError(
                    "CUDA is not visible to PyTorch. Fix the NVIDIA driver/PyTorch CUDA install, "
                    "or re-run with --device cpu."
                )

        if primary_ready and not args.force:
            print()
            print("Subtitle file already exists. Use --force to regenerate.")
        else:
            print()
            print(f"Extracting mono 16 kHz lossy {args.audio_format} audio...")
            extract_audio(video_path, audio_path, args.audio_format, args.force)

            print()
            print("Running Whisper...")
            run_whisper(audio_path, sidecar_srt_path, subs_dir, paths, args)
            finalize_subtitle_pair(
                sidecar_srt_path,
                archive_srt_path,
                args,
                is_english=False,
                label="primary",
            )

        if make_english_subs:
            if subtitle_pair_ready(english_sidecar_srt_path, english_archive_srt_path) and not args.force:
                print()
                print("English subtitle file already exists. Use --force to regenerate.")
            elif uses_openai_english_translation(args):
                if not sidecar_srt_path.exists():
                    raise RuntimeError(
                        "primary subtitles are required before OpenAI English translation can run."
                    )

                print()
                print("Generating English subtitles from the full compacted primary SRT with OpenAI...")
                translate_srt_with_openai(sidecar_srt_path, english_sidecar_srt_path, args)
                ensure_matching_subtitle_timing_pair(
                    sidecar_srt_path,
                    english_sidecar_srt_path,
                    english_archive_srt_path,
                    args,
                    label="English",
                    force=False,
                )
            else:
                print()
                print("Generating English subtitles from Dutch audio...")
                extract_audio(video_path, audio_path, args.audio_format, args.force)
                run_whisper(
                    audio_path,
                    english_sidecar_srt_path,
                    subs_dir,
                    paths,
                    args,
                    task="translate",
                    language=args.language,
                    model=english_model(args),
                )
                finalize_subtitle_pair(
                    english_sidecar_srt_path,
                    english_archive_srt_path,
                    args,
                    is_english=True,
                    label="English",
                )

        if not args.keep_audio and audio_path.exists():
            audio_path.unlink()

        if args.no_play:
            print_done(
                video_path,
                audio_path,
                sidecar_srt_path,
                archive_srt_path,
                make_english_subs=make_english_subs,
                english_sidecar_srt_path=english_sidecar_srt_path,
                english_archive_srt_path=english_archive_srt_path,
            )
        else:
            print()
            print("Opening in mpv with subtitles...")
            srt_paths = [sidecar_srt_path]
            if make_english_subs:
                srt_paths.append(english_sidecar_srt_path)
            play_video(video_path, srt_paths, args)

    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
