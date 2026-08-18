#!/usr/bin/env pythonw
r"""Windows-native launcher for the managed YouTube library application.

Example: `python .\yt_whisper_library.pyw`.
"""

from __future__ import annotations

from yt_whisper_subs import library_bootstrap


if __name__ == "__main__":
    raise SystemExit(library_bootstrap.main())
