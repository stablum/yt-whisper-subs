"""Stage durable yield replacements without exposing partially written files.

Example: `atomic_copy(sidecar, archive)` preserves the old archive on failure.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def staged_path(target: Path) -> Iterator[Path]:
    """Keep a uniquely named staging file on the target's filesystem.

    Example: write or extract into `stage`, then `stage.replace(target)`.
    """

    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f"{target.stem}.staging-", suffix=target.suffix, dir=target.parent)
    os.close(descriptor)
    stage = Path(name)
    try:
        yield stage
    finally:
        stage.unlink(missing_ok=True)


def atomic_write(target: Path, text: str) -> None:
    """Promote UTF-8 text only after the full staged write completes.

    Example: `atomic_write(marker, "Subtitles changed\n")`.
    """

    with staged_path(target) as stage:
        with stage.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        stage.replace(target)


def atomic_copy(source: Path, target: Path) -> None:
    """Preserve metadata while replacing a yield from its validated source.

    Example: a copy failure leaves the previous subtitle archive intact.
    """

    with staged_path(target) as stage:
        shutil.copy2(source, stage)
        with stage.open("r+b") as stream:
            os.fsync(stream.fileno())
        stage.replace(target)


def stale_path(target: Path) -> Path:
    """Locate a durable dependency marker beside the authoritative yield.

    Example: `x.en.srt.stale` prevents obsolete English from being reused.
    """

    return target.with_name(f"{target.name}.stale")
