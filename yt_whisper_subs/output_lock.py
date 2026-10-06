"""Coordinate application and pipeline ownership through OS file locks.

Example: `with OutputLock(root, "pipeline"):` serializes CLI and GUI writers.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path
from types import TracebackType
from typing import BinaryIO

from yt_whisper_subs import cfg

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class OutputBusyError(RuntimeError):
    """Report another live owner without treating its lock as stale.

    Example: a second library launch leaves the first instance untouched.
    """


class OutputLock:
    """Hold one nonblocking OS lock until release or process termination.

    Example: the library lock spans the event loop; pipeline locks span writes.
    """

    def __init__(self, root: Path, name: str) -> None:
        """Select the canonical root's application or pipeline lock.

        Example: `OutputLock(root, "library")` owns the native event loop.
        """

        self._path = cfg.output_scratch_dir(root.resolve()) / f"{name}.lock"
        self._stream: BinaryIO | None = None

    def __enter__(self) -> OutputLock:
        """Acquire ownership before touching the catalog or pipeline yields.

        Example: an existing owner raises `OutputBusyError` immediately.
        """

        self._path.parent.mkdir(parents=True, exist_ok=True)
        stream = self._path.open("a+b")
        if self._path.stat().st_size == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise OutputBusyError("Another process is already using this output root") from exc
            raise
        self._stream = stream
        return self

    def __exit__(self, _kind: type[BaseException] | None, _error: BaseException | None, _trace: TracebackType | None) -> None:
        """Release ownership without unlinking a lock another process may open.

        Example: the operating system also releases it after a crash.
        """

        if self._stream:
            self._stream.close()
            self._stream = None
