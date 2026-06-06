"""Local Whisper execution and model-cache validation.

Example: `whisper_local.run_whisper(audio, srt, subs_dir, paths, args)`.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from yt_whisper_subs import proc


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
    """Run the Whisper CLI and move the generated SRT to its final path.

    Example: `run_whisper(audio, sidecar_srt, subs_dir, paths, args)`.
    """

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
        proc.run(whisper_cmd)

        generated_srt = tmp_output_dir / f"{audio_path.stem}.srt"
        if not generated_srt.exists():
            raise RuntimeError(f"Whisper finished, but no .srt file was found at: {generated_srt}")

        if srt_path.exists():
            srt_path.unlink()
        shutil.move(str(generated_srt), str(srt_path))


def whisper_cache_root() -> Path:
    """Return the cache directory used by OpenAI Whisper model downloads.

    Example: `whisper_cache_root() / "model.pt"`.
    """

    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "whisper"


def whisper_model_url(paths: dict[str, Path], model: str) -> str | None:
    """Ask the installed Whisper package for the download URL of a model.

    Example: `whisper_model_url(paths, "turbo")`.
    """

    result = subprocess.run(
        [
            str(paths["python"]),
            "-c",
            "import sys, whisper; print(whisper._MODELS[sys.argv[1]])",
            model,
        ],
        env=proc.child_process_env(),
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
    """Stream a file through SHA-256 without loading large model files at once.

    Example: `sha256_file(cache_path)`.
    """

    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def remove_invalid_whisper_model_cache(paths: dict[str, Path], model: str) -> None:
    """Delete empty or hash-mismatched Whisper model cache files before running.

    Example: `remove_invalid_whisper_model_cache(paths, "turbo")`.
    """

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
