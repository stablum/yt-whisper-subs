#!/usr/bin/env python
"""Download a video, generate local Whisper subtitles, and optionally play it.

This script does not depend on YouTube captions. It downloads a YouTube video
with yt-dlp or accepts a local video file, extracts 16 kHz mono WAV audio with
ffmpeg, runs OpenAI Whisper locally, writes an SRT file, and can open the video
in mpv with the generated subtitle file attached.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


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

    parser.add_argument("--out-dir", default="yt-whisper-output", help="Output directory.")
    parser.add_argument(
        "--language",
        default="nl",
        help="Whisper language code, or 'auto' to let Whisper detect it. Default: nl.",
    )
    parser.add_argument("--model", choices=MODEL_CHOICES, default="medium")
    parser.add_argument("--task", choices=("transcribe", "translate"), default="transcribe")
    parser.add_argument("--device", choices=("cuda", "cpu"), default="cuda")
    parser.add_argument(
        "--torch-index-url",
        default="https://download.pytorch.org/whl/cu124",
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
        help="Create/update the private Python venv and install torch, yt-dlp, and openai-whisper.",
    )
    parser.add_argument("--no-play", action="store_true", help="Only create subtitles; do not open mpv.")
    parser.add_argument("--force", action="store_true", help="Regenerate subtitles even if they already exist.")
    parser.add_argument("--keep-audio", action="store_true", help="Keep the temporary extracted WAV file.")
    parser.add_argument(
        "--cookies-from-browser",
        help="Pass through to yt-dlp, for example: firefox, chrome, edge.",
    )

    args = parser.parse_args()
    explicit_sources = [value for value in (args.source, args.url, args.video_file) if value]
    if len(explicit_sources) != 1:
        parser.error("provide exactly one source: positional SOURCE, --url, or --video-file")

    if args.source:
        if looks_like_url(args.source):
            args.url = args.source
        else:
            args.video_file = args.source

    return args


def looks_like_url(value: str) -> bool:
    lowered = value.lower()
    return lowered.startswith(("http://", "https://"))


def local_app_data() -> Path:
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        return Path(local_appdata)
    if os.name == "nt":
        return Path.home() / "AppData" / "Local"
    return Path.home() / ".local" / "share"


def venv_paths() -> dict[str, Path]:
    tool_dir = local_app_data() / "yt-whisper-subs"
    venv_dir = tool_dir / "venv"
    scripts_dir = venv_dir / ("Scripts" if os.name == "nt" else "bin")
    exe_suffix = ".exe" if os.name == "nt" else ""

    return {
        "tool_dir": tool_dir,
        "venv_dir": venv_dir,
        "python": scripts_dir / f"python{exe_suffix}",
        "whisper": scripts_dir / f"whisper{exe_suffix}",
    }


def command_text(cmd: list[str | os.PathLike[str]]) -> str:
    return " ".join(str(part) for part in cmd)


def run(cmd: list[str | os.PathLike[str]], *, capture_stdout: bool = False) -> subprocess.CompletedProcess[str]:
    print()
    print(f"> {command_text(cmd)}")
    stdout = subprocess.PIPE if capture_stdout else None
    result = subprocess.run(
        [str(part) for part in cmd],
        check=False,
        text=True,
        stdout=stdout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"command failed with exit code {result.returncode}: {cmd[0]}")
    return result


def require_command(name: str) -> None:
    if shutil.which(name) is None:
        raise RuntimeError(f"required command not found: {name}")


def install_tools() -> None:
    if shutil.which("scoop") is None:
        raise RuntimeError("scoop not found. Install ffmpeg and mpv manually, or install scoop first.")
    run(["scoop", "install", "ffmpeg", "mpv"])
    run(["scoop", "update", "ffmpeg", "mpv"])


def ensure_python_deps(paths: dict[str, Path], args: argparse.Namespace) -> None:
    paths["tool_dir"].mkdir(parents=True, exist_ok=True)

    if args.install_python_deps or not paths["python"].exists():
        print(f"Creating/updating Python venv in: {paths['venv_dir']}")
        if not paths["python"].exists():
            run([sys.executable, "-m", "venv", paths["venv_dir"]])

        run([paths["python"], "-m", "pip", "install", "--upgrade", "pip", "wheel", "setuptools"])

        torch_cmd = [
            paths["python"],
            "-m",
            "pip",
            "install",
            "--upgrade",
            "torch",
            "torchvision",
            "torchaudio",
        ]
        if args.device == "cuda":
            torch_cmd += ["--index-url", args.torch_index_url]
        run(torch_cmd)

        run([paths["python"], "-m", "pip", "install", "--upgrade", "yt-dlp", "openai-whisper"])

    if not paths["python"].exists():
        raise RuntimeError(f"Whisper Python venv not found at {paths['venv_dir']}. Re-run with --install-python-deps.")
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


def download_video(url: str, out_dir: Path, paths: dict[str, Path], cookies_from_browser: str | None) -> Path:
    media_dir = out_dir / "media"
    media_dir.mkdir(parents=True, exist_ok=True)

    template = media_dir / "%(title).180B [%(id)s].%(ext)s"
    cmd: list[str | os.PathLike[str]] = [
        paths["python"],
        "-m",
        "yt_dlp",
        "--no-playlist",
        "--windows-filenames",
        "-f",
        "bv*+ba/b",
        "--merge-output-format",
        "mkv",
        "--print",
        "after_move:filepath",
        "-o",
        template,
    ]

    if cookies_from_browser:
        cmd += ["--cookies-from-browser", cookies_from_browser]

    cmd.append(url)
    result = run(cmd, capture_stdout=True)
    lines = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    existing_paths = [Path(line) for line in lines if Path(line).exists()]
    if not existing_paths:
        raise RuntimeError("could not determine downloaded video path from yt-dlp output")
    return existing_paths[-1].resolve()


def extract_audio(video_path: Path, audio_path: Path, force: bool) -> None:
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
            audio_path,
        ]
    )


def run_whisper(
    audio_path: Path,
    srt_path: Path,
    subs_dir: Path,
    paths: dict[str, Path],
    args: argparse.Namespace,
) -> None:
    whisper_cmd: list[str | os.PathLike[str]] = [
        paths["whisper"],
        audio_path,
        "--model",
        args.model,
        "--task",
        args.task,
        "--output_format",
        "srt",
        "--output_dir",
        subs_dir,
        "--device",
        args.device,
        "--fp16",
        "True" if args.device == "cuda" else "False",
    ]

    if args.language and args.language != "auto":
        whisper_cmd += ["--language", args.language]

    run(whisper_cmd)

    generated_srt = subs_dir / f"{audio_path.stem}.srt"
    if not generated_srt.exists():
        raise RuntimeError(f"Whisper finished, but no .srt file was found at: {generated_srt}")

    if generated_srt.resolve() != srt_path.resolve():
        shutil.move(str(generated_srt), str(srt_path))


def play_video(video_path: Path, srt_path: Path) -> None:
    run(["mpv", f"--sub-file={srt_path}", "--sub-auto=no", video_path])


def main() -> int:
    args = parse_args()
    paths = venv_paths()

    try:
        if args.install_tools:
            install_tools()

        require_command("ffmpeg")
        if not args.no_play:
            require_command("mpv")

        ensure_python_deps(paths, args)

        print()
        print("Checking PyTorch CUDA visibility...")
        if args.device == "cuda" and not check_cuda(paths):
            raise RuntimeError(
                "CUDA is not visible to PyTorch. Fix the NVIDIA driver/PyTorch CUDA install, "
                "or re-run with --device cpu."
            )

        out_dir = Path(args.out_dir).expanduser().resolve()
        audio_dir = out_dir / "audio"
        subs_dir = out_dir / "subs"
        audio_dir.mkdir(parents=True, exist_ok=True)
        subs_dir.mkdir(parents=True, exist_ok=True)

        if args.url:
            print()
            print("Downloading video...")
            video_path = download_video(args.url, out_dir, paths, args.cookies_from_browser)
        else:
            video_path = resolve_video_path(args.video_file)

        video_base = video_path.stem
        audio_path = audio_dir / f"{video_base}.wav"
        srt_path = subs_dir / f"{video_base}.srt"

        print()
        print(f"Video: {video_path}")
        print(f"Audio: {audio_path}")
        print(f"SRT:   {srt_path}")

        if srt_path.exists() and not args.force:
            print()
            print("Subtitle file already exists. Use --force to regenerate.")
        else:
            print()
            print("Extracting mono 16 kHz WAV audio...")
            extract_audio(video_path, audio_path, args.force)

            print()
            print("Running Whisper...")
            run_whisper(audio_path, srt_path, subs_dir, paths, args)

        if not args.keep_audio and audio_path.exists():
            audio_path.unlink()

        if args.no_play:
            print()
            print("Done.")
            print(f"Video: {video_path}")
            print(f"Subs:  {srt_path}")
        else:
            print()
            print("Opening in mpv with subtitles...")
            play_video(video_path, srt_path)

    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
