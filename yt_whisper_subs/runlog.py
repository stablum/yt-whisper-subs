"""Run-log teeing and log path helpers.

Example: `with runlog.RunLogger(path): ...` mirrors stdout/stderr to a file.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from yt_whisper_subs import media
from yt_whisper_subs import proc
from yt_whisper_subs import youtube


class TeeStream:
    """Mirror writes to a console stream and a UTF-8 log file.

    Example: `sys.stdout = TeeStream(sys.stdout, log_file)`.
    """

    def __init__(self, primary, log_file) -> None:
        self.primary = primary
        self.log_file = log_file
        self.encoding = getattr(primary, "encoding", "utf-8")
        self.errors = getattr(primary, "errors", "replace")

    def write(self, text: str) -> int:
        """Write text to both streams and return the console write count.

        Example: `tee.write("hello")`.
        """

        written = self.primary.write(text)
        self.log_file.write(text)
        return written

    def flush(self) -> None:
        """Flush both streams so long-running tools appear live.

        Example: `tee.flush()` after progress output.
        """

        self.primary.flush()
        self.log_file.flush()

    def isatty(self) -> bool:
        """Expose terminal-ness from the primary stream.

        Example: `tee.isatty()` for console-aware libraries.
        """

        return self.primary.isatty()

    def fileno(self) -> int:
        """Expose the primary stream file descriptor for compatibility.

        Example: `tee.fileno()` when a library asks for it.
        """

        return self.primary.fileno()


class RunLogger:
    """Temporarily tee process output into the timestamped run log.

    Example: `with RunLogger(log_path): print("captured")`.
    """

    def __init__(self, log_path: Path) -> None:
        self.log_path = log_path
        self.log_file = None
        self.stdout = None
        self.stderr = None

    def __enter__(self) -> Path:
        """Open the log and activate stdout/stderr mirroring.

        Example: `with logger as path: ...`.
        """

        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_file = self.log_path.open("a", encoding="utf-8", newline="")
        self.stdout = sys.stdout
        self.stderr = sys.stderr
        sys.stdout = TeeStream(self.stdout, self.log_file)
        sys.stderr = TeeStream(self.stderr, self.log_file)
        return self.log_path

    def __exit__(self, exc_type, exc, traceback) -> None:
        """Restore process streams and close the file even on errors.

        Example: handled automatically at context exit.
        """

        try:
            sys.stdout.flush()
            sys.stderr.flush()
        finally:
            if self.stdout is not None:
                sys.stdout = self.stdout
            if self.stderr is not None:
                sys.stderr = self.stderr
            if self.log_file is not None:
                self.log_file.close()


def default_log_path(args: argparse.Namespace, out_dir: Path) -> Path:
    """Build the standard source-and-timestamp log path.

    Example: `default_log_path(args, out_dir)`.
    """

    if args.url:
        source_stem = youtube.youtube_video_id(args.url) or "url"
    else:
        source_stem = Path(args.video_file).expanduser().stem
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    return out_dir / "logs" / f"{media.safe_output_stem(source_stem)}-{timestamp}.log"


def resolve_log_path(args: argparse.Namespace, out_dir: Path) -> Path:
    """Use an explicit log path or fall back to the run-log convention.

    Example: `resolve_log_path(args, out_dir)`.
    """

    if args.log_file:
        return Path(args.log_file).expanduser().resolve()
    return default_log_path(args, out_dir)


def redacted_args(args: argparse.Namespace) -> dict[str, object]:
    """Return stable argument data for logs without environment values.

    Example: `json.dumps(redacted_args(args))`.
    """

    values = vars(args).copy()
    return {key: values[key] for key in sorted(values)}


def print_run_header(args: argparse.Namespace, out_dir: Path, log_path: Path) -> None:
    """Write invocation metadata at the start of each run log.

    Example: `print_run_header(args, out_dir, log_path)`.
    """

    print(f"Run log: {log_path}")
    print(f"Started: {time.strftime('%Y-%m-%d %H:%M:%S %z')}")
    print(f"CWD: {Path.cwd()}")
    print(f"Command: {proc.command_line_text(sys.argv)}")
    print(f"Output root: {out_dir}")
    print("Arguments:")
    print(json.dumps(redacted_args(args), ensure_ascii=False, indent=2, sort_keys=True))
