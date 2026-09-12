"""Cooperative cancellation shared by Qt workers and child-process owners.

Example: `with token.stoppable(stop): run_work()` registers an immediate stop hook.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token as ContextToken


class CancelledError(Exception):
    """Distinguish a requested stop from an operational pipeline failure.

    Example: `token.checkpoint()` raises this after the user presses Cancel.
    """


class CancellationToken:
    """Own one task's cancellation flag and currently active stop hooks.

    Example: a pipeline registers its child-process-tree terminator here.
    """

    def __init__(self) -> None:
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._stops: set[Callable[[], None]] = set()

    @property
    def cancelled(self) -> bool:
        """Report whether cancellation has already been requested.

        Example: GUI action state uses `token.cancelled` to disable repetition.
        """

        return self._cancelled.is_set()

    def cancel(self) -> bool:
        """Request cancellation once and invoke active stop hooks immediately.

        Example: cancelling a pipeline terminates its Python child process tree.
        """

        if self._cancelled.is_set():
            return False
        self._cancelled.set()
        with self._lock:
            stops = tuple(self._stops)
        for stop in stops:
            try:
                stop()
            except Exception:
                # The worker still observes the flag at its next checkpoint.
                continue
        return True

    def checkpoint(self) -> None:
        """Raise at a safe worker boundary after cancellation was requested.

        Example: progress reporting checkpoints channel loops between entries.
        """

        if self.cancelled:
            raise CancelledError("operation cancelled")

    @contextmanager
    def stoppable(self, stop: Callable[[], None]) -> Iterator[None]:
        """Register a stop hook only for the lifetime of active blocking work.

        Example: `stoppable(lambda: terminate_tree(process))` avoids orphan tools.
        """

        with self._lock:
            self._stops.add(stop)
            cancelled = self.cancelled
        if cancelled:
            stop()
        try:
            self.checkpoint()
            yield
            self.checkpoint()
        finally:
            with self._lock:
                self._stops.discard(stop)


_CURRENT: ContextVar[CancellationToken | None] = ContextVar("yt_whisper_cancel", default=None)


@contextmanager
def activate(token: CancellationToken) -> Iterator[None]:
    """Publish one token only inside its worker execution context.

    Example: `BackgroundTask.run()` activates its token around the service call.
    """

    context_token: ContextToken[CancellationToken | None] = _CURRENT.set(token)
    try:
        yield
    finally:
        _CURRENT.reset(context_token)


def current() -> CancellationToken | None:
    """Return the active worker token without coupling services to Qt.

    Example: the pipeline downloader registers its child process when available.
    """

    return _CURRENT.get()
