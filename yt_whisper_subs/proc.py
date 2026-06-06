"""Process, stdio, command, and script-local virtualenv helpers.

Example: `proc.run(["python", "--version"])` prints and executes a command.
"""

from __future__ import annotations

import argparse
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TextIO

from yt_whisper_subs import cfg


_OUTPUT_POLL_SECONDS = 0.2


def venv_paths() -> dict[str, Path]:
    """Return the project-local Python and Whisper executable paths.

    Example: `venv_paths()["python"]`.
    """

    venv_dir = cfg.PROJECT_DIR / ".venv"
    scripts_dir = venv_dir / ("Scripts" if os.name == "nt" else "bin")
    exe_suffix = ".exe" if os.name == "nt" else ""

    return {
        "script_dir": cfg.PROJECT_DIR,
        "venv_dir": venv_dir,
        "python": scripts_dir / f"python{exe_suffix}",
        "whisper": scripts_dir / f"whisper{exe_suffix}",
    }


def command_text(cmd: list[str | os.PathLike[str]]) -> str:
    """Format a subprocess command for readable logging.

    Example: `command_text(["uv", "run"])`.
    """

    return " ".join(str(part) for part in cmd)


def command_line_text(argv: list[str]) -> str:
    """Format the top-level invocation exactly enough for run logs.

    Example: `command_line_text(sys.argv)`.
    """

    return " ".join(argv)


def configure_stdio() -> None:
    """Force UTF-8 console streams when the host stream supports it.

    Example: `configure_stdio()` before parsing args.
    """

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def child_process_env() -> dict[str, str]:
    """Build a subprocess env that makes Python tools emit UTF-8 text.

    Example: `subprocess.run(cmd, env=child_process_env())`.
    """

    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _terminate_process(process: subprocess.Popen[str]) -> None:
    """Stop a child process before propagating wrapper interruption.

    Example: `_terminate_process(process)` after Ctrl-C.
    """

    if process.poll() is not None:
        return

    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _read_stream_chars(stream: TextIO, output_q: queue.Queue[str | None]) -> None:
    """Move blocking pipe reads into a queue so the parent can stay responsive.

    Example: `_read_stream_chars(process.stdout, output_q)` in a reader thread.
    """

    try:
        while chunk := stream.read(1):
            output_q.put(chunk)
    finally:
        output_q.put(None)


def _stream_process_output(
    process: subprocess.Popen[str],
    *,
    capture_stdout: bool,
    silence_seconds: float | None,
) -> tuple[int, str | None]:
    """Drain subprocess output while reporting long periods with no text.

    Example: `_stream_process_output(process, capture_stdout=True, silence_seconds=60)`.
    """

    assert process.stdout is not None
    output_q: queue.Queue[str | None] = queue.Queue()
    reader = threading.Thread(
        target=_read_stream_chars,
        args=(process.stdout, output_q),
        daemon=True,
        name="yt-whisper-subs-output-reader",
    )
    reader.start()

    captured_parts: list[str] = []
    last_output_time = time.monotonic()
    reader_done = False
    silence_limit = silence_seconds or 0.0

    while not reader_done or process.poll() is None:
        try:
            chunk = output_q.get(timeout=_OUTPUT_POLL_SECONDS)
        except queue.Empty:
            now = time.monotonic()
            silent_for = now - last_output_time
            if silence_limit > 0 and silent_for >= silence_limit:
                print(f"[subprocess still running; no output for {silent_for:.0f}s]")
                sys.stdout.flush()
                last_output_time = now
            continue

        if chunk is None:
            reader_done = True
            continue

        if capture_stdout:
            captured_parts.append(chunk)
        sys.stdout.write(chunk)
        if chunk in {"\n", "\r"}:
            sys.stdout.flush()
        last_output_time = time.monotonic()

    returncode = process.wait()
    reader.join(timeout=1)
    stdout_text = "".join(captured_parts) if capture_stdout else None
    return returncode, stdout_text


def run(
    cmd: list[str | os.PathLike[str]],
    *,
    capture_stdout: bool = False,
    stream_stdout: bool = False,
    check: bool = True,
    silence_seconds: float | None = cfg.DEFAULT_SUBPROCESS_SILENCE_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Execute a command while preserving live output in the run log.

    Example: `run(["ffmpeg", "-version"], check=False)`.
    """

    configure_stdio()
    print()
    print(f"> {command_text(cmd)}")
    env = child_process_env()
    command = [str(part) for part in cmd]

    if capture_stdout and not stream_stdout:
        result = subprocess.run(
            command,
            env=env,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    else:
        process = subprocess.Popen(
            command,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        try:
            returncode, stdout_text = _stream_process_output(
                process,
                capture_stdout=capture_stdout,
                silence_seconds=silence_seconds,
            )
        except BaseException:
            _terminate_process(process)
            raise
        result = subprocess.CompletedProcess(command, returncode, stdout=stdout_text)

    if check and result.returncode != 0:
        raise RuntimeError(f"command failed with exit code {result.returncode}: {cmd[0]}")
    return result


def require_command(name: str) -> None:
    """Fail early when a required external executable is unavailable.

    Example: `require_command("ffmpeg")`.
    """

    if shutil.which(name) is None:
        raise RuntimeError(f"required command not found: {name}")


def install_tools() -> None:
    """Install or update external tools through Scoop on Windows.

    Example: `install_tools()` for `--install-tools`.
    """

    if shutil.which("scoop") is None:
        raise RuntimeError("scoop not found. Install uv, ffmpeg, and mpv manually, or install scoop first.")
    run(["scoop", "install", "uv", "ffmpeg", "mpv"])
    run(["scoop", "update", "uv", "ffmpeg", "mpv"])


def get_python_minor_version(python_exe: Path) -> str | None:
    """Read a Python executable's major.minor version without importing project code.

    Example: `get_python_minor_version(paths["python"])`.
    """

    if not python_exe.exists():
        return None

    result = subprocess.run(
        [str(python_exe), "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
        env=child_process_env(),
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
    """Normalize a requested Python version to the managed venv granularity.

    Example: `requested_python_minor_version("3.14.0")`.
    """

    parts = version.split(".")
    if len(parts) < 2:
        return version
    return ".".join(parts[:2])


def ensure_python_deps(paths: dict[str, Path], args: argparse.Namespace) -> None:
    """Create or refresh the script-local Whisper virtual environment.

    Example: `ensure_python_deps(proc.venv_paths(), args)`.
    """

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

        torch_cmd: list[str | os.PathLike[str]] = [
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
    """Probe PyTorch CUDA visibility inside the managed venv.

    Example: `check_cuda(paths)` before Whisper on GPU.
    """

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
