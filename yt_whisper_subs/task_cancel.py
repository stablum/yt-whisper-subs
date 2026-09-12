"""Cooperative pause and cancellation shared by workers and child processes.

Example: `with control.controllable(stop, pause, resume): run_work()`.
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


class TaskControl:
    """Own one task's pause/cancel state and active process hooks.

    Example: a pipeline registers pause, resume, and termination hooks here.
    """

    def __init__(self) -> None:
        self._cancelled = threading.Event()
        self._paused = threading.Event()
        self._condition = threading.Condition()
        self._stops: set[Callable[[], None]] = set()
        self._pauses: set[Callable[[], None]] = set()
        self._resumes: set[Callable[[], None]] = set()

    @property
    def cancelled(self) -> bool:
        """Report whether cancellation has already been requested.

        Example: GUI action state uses `token.cancelled` to disable repetition.
        """

        return self._cancelled.is_set()

    @property
    def paused(self) -> bool:
        """Report whether the task is intentionally suspended.

        Example: the GUI changes Pause to Resume while this is true.
        """

        return self._paused.is_set()

    def pause(self) -> bool:
        """Suspend cooperative work and every registered child-process tree.

        Example: pausing Whisper freezes its CLI process and descendants.
        """

        with self._condition:
            if self.cancelled or self.paused:
                return False
            self._paused.set()
            for pause in tuple(self._pauses):
                pause()
        return True

    def resume(self) -> bool:
        """Resume cooperative work and every registered child-process tree.

        Example: a second click continues the suspended Whisper process.
        """

        with self._condition:
            if self.cancelled or not self.paused:
                return False
            for resume in tuple(self._resumes):
                resume()
            self._paused.clear()
            self._condition.notify_all()
        return True

    def cancel(self) -> bool:
        """Request cancellation once and invoke active stop hooks immediately.

        Example: cancelling a pipeline terminates its Python child process tree.
        """

        if self._cancelled.is_set():
            return False
        self._cancelled.set()
        with self._condition:
            self._condition.notify_all()
            for stop in tuple(self._stops):
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

        with self._condition:
            while self.paused and not self.cancelled:
                self._condition.wait()
            if self.cancelled:
                raise CancelledError("operation cancelled")

    @contextmanager
    def controllable(
        self,
        stop: Callable[[], None],
        pause: Callable[[], None],
        resume: Callable[[], None],
    ) -> Iterator[None]:
        """Register process controls only for active blocking work.

        Example: a child tree becomes immediately pausable and cancellable.
        """

        with self._condition:
            self._stops.add(stop)
            self._pauses.add(pause)
            self._resumes.add(resume)
            cancelled = self.cancelled
            paused = self.paused
            if cancelled:
                stop()
            elif paused:
                pause()
        try:
            self.checkpoint()
            yield
            self.checkpoint()
        finally:
            with self._condition:
                self._stops.discard(stop)
                self._pauses.discard(pause)
                self._resumes.discard(resume)


_CURRENT: ContextVar[TaskControl | None] = ContextVar("yt_whisper_task_control", default=None)


@contextmanager
def activate(control: TaskControl) -> Iterator[None]:
    """Publish one control only inside its worker execution context.

    Example: `BackgroundTask.run()` activates its token around the service call.
    """

    context_token: ContextToken[TaskControl | None] = _CURRENT.set(control)
    try:
        yield
    finally:
        _CURRENT.reset(context_token)


def current() -> TaskControl | None:
    """Return the active worker control without coupling services to Qt.

    Example: the pipeline downloader registers its child process when available.
    """

    return _CURRENT.get()
