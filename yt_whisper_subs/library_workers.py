"""One-at-a-time Qt worker primitives for slow network and pipeline work.

Example: `BackgroundTask(fn).signals.finished.connect(handler)`.
"""

from __future__ import annotations

import traceback
from collections.abc import Callable
from typing import Any

from PySide6 import QtCore


class TaskSignals(QtCore.QObject):
    """Carry background progress, results, and failures to the GUI thread.

    Example: `signals.progress.connect(status_bar.showMessage)`.
    """

    started = QtCore.Signal()
    progress = QtCore.Signal(str)
    finished = QtCore.Signal(object)
    failed = QtCore.Signal(str, str)


class BackgroundTask(QtCore.QRunnable):
    """Run a report-aware callable without freezing native widgets.

    Example: `pool.start(BackgroundTask(service.check_all))`.
    """

    def __init__(self, fn: Callable[[Callable[[str], None]], Any]) -> None:
        super().__init__()
        self._fn = fn
        self.signals = TaskSignals()

    @QtCore.Slot()
    def run(self) -> None:
        """Execute once and translate exceptions into a display-safe signal.

        Example: Qt's thread pool invokes `run()`.
        """

        self.signals.started.emit()
        try:
            result = self._fn(self.signals.progress.emit)
        except Exception as exc:
            self.signals.failed.emit(str(exc), traceback.format_exc())
        else:
            self.signals.finished.emit(result)
