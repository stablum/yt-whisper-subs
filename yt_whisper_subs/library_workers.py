"""One-at-a-time Qt worker primitives for slow network and pipeline work.

Example: `BackgroundTask(fn).signals.finished.connect(handler)`.
"""

from __future__ import annotations

import traceback
from collections.abc import Callable
from typing import Any

from PySide6 import QtCore

from yt_whisper_subs import task_cancel


class TaskSignals(QtCore.QObject):
    """Carry background progress, results, and failures to the GUI thread.

    Example: `signals.progress.connect(status_bar.showMessage)`.
    """

    started = QtCore.Signal()
    progress = QtCore.Signal(str)
    finished = QtCore.Signal(object)
    failed = QtCore.Signal(str, str)
    cancelled = QtCore.Signal()


class BackgroundTask(QtCore.QRunnable):
    """Run a report-aware callable without freezing native widgets.

    Example: `pool.start(BackgroundTask(service.check_all))`.
    """

    def __init__(self, fn: Callable[[Callable[[str], None]], Any]) -> None:
        super().__init__()
        self._fn = fn
        self._cancel = task_cancel.CancellationToken()
        self.signals = TaskSignals()

    @property
    def cancel_requested(self) -> bool:
        """Expose whether this worker is already stopping.

        Example: the Cancel button disables after its first click.
        """

        return self._cancel.cancelled

    def cancel(self) -> bool:
        """Request a cooperative stop without terminating the Qt thread.

        Example: active child tools stop while the reusable worker pool survives.
        """

        return self._cancel.cancel()

    @QtCore.Slot()
    def run(self) -> None:
        """Execute once and translate exceptions into a display-safe signal.

        Example: Qt's thread pool invokes `run()`.
        """

        self.signals.started.emit()

        def report(message: str) -> None:
            """Make every progress emission a cheap cancellation checkpoint.

            Example: a channel loop stops before emitting its next status line.
            """

            self._cancel.checkpoint()
            self.signals.progress.emit(message)

        try:
            with task_cancel.activate(self._cancel):
                self._cancel.checkpoint()
                result = self._fn(report)
                self._cancel.checkpoint()
        except task_cancel.CancelledError:
            self.signals.cancelled.emit()
        except Exception as exc:
            self.signals.failed.emit(str(exc), traceback.format_exc())
        else:
            self.signals.finished.emit(result)
