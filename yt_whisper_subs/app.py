"""Application entry point that wires CLI, logging, and pipeline modules.

Example: `app.main()` is used by both the script and `python -m`.
"""

from __future__ import annotations

import sys

from yt_whisper_subs import cli
from yt_whisper_subs import pipeline
from yt_whisper_subs import proc
from yt_whisper_subs import runlog


def main() -> int:
    """Run the command-line application and convert failures into exit code 1.

    Example: `raise SystemExit(main())`.
    """

    proc.configure_stdio()
    args = cli.parse_args()
    paths = proc.venv_paths()
    out_dir = pipeline.resolve_output_dir(args.out_dir)
    log_path = runlog.resolve_log_path(args, out_dir)

    with runlog.RunLogger(log_path):
        runlog.print_run_header(args, out_dir, log_path)
        try:
            return pipeline.run_pipeline(args, paths, out_dir, log_path)
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
