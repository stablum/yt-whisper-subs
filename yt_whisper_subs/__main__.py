"""Module execution entry point for `python -m yt_whisper_subs`.

Example: `python -m yt_whisper_subs --help`.
"""

from __future__ import annotations

from yt_whisper_subs import app


if __name__ == "__main__":
    raise SystemExit(app.main())
