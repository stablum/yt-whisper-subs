"""YouTube ID naming, cache lookup, migration, and yt-dlp download logic.

Example: `youtube.latest_downloaded_video(video_dir, url)`.
"""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path
from urllib.parse import parse_qs
from urllib.parse import urlparse

from yt_whisper_subs import cfg
from yt_whisper_subs import proc

ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
INTERMEDIATE_FORMAT_RE = re.compile(r"\.f\d+\.(?:m4a|mkv|mp4|webm)$", re.IGNORECASE)
YOUTUBE_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def clean_output_line(line: str) -> str:
    """Strip terminal control sequences from yt-dlp output lines.

    Example: `clean_output_line(raw_line)`.
    """

    return ANSI_ESCAPE_RE.sub("", line).strip().strip('"')


def normalize_youtube_video_id(value: str | None) -> str | None:
    """Validate a potential 11-character YouTube video ID.

    Example: `normalize_youtube_video_id("dQw4w9WgXcQ")`.
    """

    if not value:
        return None

    candidate = value.strip().strip("/")
    if YOUTUBE_VIDEO_ID_RE.fullmatch(candidate):
        return candidate
    return None


def youtube_video_id(url: str) -> str | None:
    """Extract the canonical YouTube video ID from supported URL shapes.

    Example: `youtube_video_id("https://youtu.be/dQw4w9WgXcQ")`.
    """

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


def youtube_id_matches_video_file(path: Path, video_id: str) -> bool:
    """Check canonical and old title-with-ID cache filename forms.

    Example: `youtube_id_matches_video_file(path, video_id)`.
    """

    return path.stem == video_id or path.stem.endswith(f"[{video_id}]")


def canonicalize_youtube_video_filename(video_path: Path, video_id: str) -> Path:
    """Rename legacy title-based video yields to the ID-only convention.

    Example: `canonicalize_youtube_video_filename(path, "abc123abc12")`.
    """

    if video_path.stem == video_id:
        return video_path.resolve()

    target_path = video_path.with_name(f"{video_id}{video_path.suffix}")
    if target_path.exists() and target_path.resolve() != video_path.resolve():
        print(f"Using existing video-id video filename instead of legacy title filename: {target_path}")
        return target_path.resolve()

    video_path.replace(target_path)
    print(f"Renamed video to video-id filename: {target_path}")
    return target_path.resolve()


def legacy_youtube_named_files(directory: Path, video_id: str, suffix: str) -> list[Path]:
    """Find old title-derived yields for one YouTube ID and suffix.

    Example: `legacy_youtube_named_files(video_dir, video_id, ".mkv")`.
    """

    if not directory.exists():
        return []
    canonical_name = f"{video_id}{suffix}"
    return sorted(
        [
            path
            for path in directory.iterdir()
            if path.is_file()
            and path.name != canonical_name
            and path.name.endswith(f"[{video_id}]{suffix}")
        ],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def migrate_legacy_youtube_file(directory: Path, video_id: str, suffix: str, label: str) -> None:
    """Rename one matching legacy yield when no canonical target exists.

    Example: `migrate_legacy_youtube_file(video_dir, video_id, ".srt", "subtitle")`.
    """

    target_path = directory / f"{video_id}{suffix}"
    for legacy_path in legacy_youtube_named_files(directory, video_id, suffix):
        if target_path.exists():
            print(
                f"Leaving legacy {label} filename in place because the video-id filename already exists: "
                f"{legacy_path}"
            )
            continue

        target_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.replace(target_path)
        print(f"Renamed legacy {label} to video-id filename: {target_path}")


def migrate_legacy_youtube_yields_to_video_id(
    video_id: str,
    video_dir: Path,
    audio_dir: Path,
    subs_dir: Path,
) -> None:
    """Migrate every known sidecar/archive/audio yield to ID-only names.

    Example: `migrate_legacy_youtube_yields_to_video_id(video_id, videos, audio, subs)`.
    """

    for suffix in cfg.MEDIA_SUFFIXES:
        migrate_legacy_youtube_file(video_dir, video_id, suffix, "video")

    for suffix in (".srt", ".en.srt", ".uncompact.srt", ".en.uncompact.srt", ".en.partial.json"):
        migrate_legacy_youtube_file(video_dir, video_id, suffix, "video sidecar subtitle")
        migrate_legacy_youtube_file(subs_dir, video_id, suffix, "subtitle archive")

    for audio_format in cfg.AUDIO_FORMAT_CHOICES:
        migrate_legacy_youtube_file(audio_dir, video_id, f".{audio_format}", "audio")


def latest_downloaded_video(video_dir: Path, url: str) -> Path | None:
    """Return an exact YouTube-ID cache hit, never a newest-file guess.

    Example: `latest_downloaded_video(video_dir, url)`.
    """

    video_id = youtube_video_id(url)
    files = [
        path
        for path in video_dir.iterdir()
        if path.is_file()
        and path.suffix.lower() in cfg.MEDIA_SUFFIXES
        and not path.name.endswith(".part")
        and not INTERMEDIATE_FORMAT_RE.search(path.name)
    ]

    if video_id:
        canonical_matches = [path for path in files if path.stem == video_id]
        if canonical_matches:
            return max(canonical_matches, key=lambda path: path.stat().st_mtime).resolve()

        legacy_matches = [path for path in files if youtube_id_matches_video_file(path, video_id)]
        if legacy_matches:
            return max(legacy_matches, key=lambda path: path.stat().st_mtime).resolve()

    return None


def download_video(url: str, video_dir: Path, paths: dict[str, Path], args: argparse.Namespace) -> Path:
    """Download a compressed source video with yt-dlp and return the final path.

    Example: `download_video(url, video_dir, paths, args)`.
    """

    video_dir.mkdir(parents=True, exist_ok=True)

    template = video_dir / "%(id)s.%(ext)s"
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
    result = proc.run(cmd, capture_stdout=True, stream_stdout=True, check=False)
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
