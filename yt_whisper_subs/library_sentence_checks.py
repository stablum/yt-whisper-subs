"""Run durable sentence corrections independently of the practice window.

Example: remote translations queue behind one worker, while local dictation runs independently.
"""

import os
import uuid

import psutil
from PySide6 import QtCore
from PySide6 import QtWidgets

from yt_whisper_subs import library_workers
from yt_whisper_subs import sentence_service
from yt_whisper_subs import task_cancel


class SentenceChecks(QtCore.QObject):
    """Own one correction lane per catalog and task type; e.g. local dictation never waits for a translation API timeout."""

    changed = QtCore.Signal(int)

    @classmethod
    def shared(cls, db, *, local: bool = False):
        """Keep lifetime with QApplication rather than a dialog; e.g. submitted answers continue correcting while practice is closed."""

        app = QtWidgets.QApplication.instance()
        key = os.path.normcase(str(db.path.resolve())) + (":dictation" if local else ":translation")
        for worker in app.findChildren(cls):
            if worker.objectName() == key:
                return worker
        worker = cls(db, app, local=local)
        worker.setObjectName(key)
        app.aboutToQuit.connect(worker.stop)
        return worker

    def __init__(self, db, parent=None, *, local: bool = False) -> None:
        """Construct without starting paid work; e.g. statistics-only tests and views remain read-only."""

        super().__init__(parent)
        self._db = db
        self._local = local
        self._engine = sentence_service.SentenceService(db)
        self._owner = f"{os.getpid()}:{psutil.Process().create_time()}:{uuid.uuid4().hex}"
        self._task = None
        self._job = None
        self._stopped = False
        self._wake = QtCore.QTimer(self)
        self._wake.setSingleShot(True)
        self._wake.timeout.connect(self._pump)

    def resume(self, engine=None) -> None:
        """Kick durable pending work without duplicating it; e.g. startup resumes a queued answer after a crash."""

        if engine is not None:
            self._engine = engine
        if not self._stopped:
            self._wake.start(0)

    def retry(self, presentation_id: int, engine) -> None:
        """Retry only on an explicit gesture; e.g. network failure never changes the immutable answer."""

        self._db.sentence_check_retry(presentation_id)
        self.changed.emit(presentation_id)
        self.resume(engine)

    def _pump(self) -> None:
        """Take one oldest pending answer at a time; e.g. several quick submissions queue instead of issuing a burst of requests."""

        if self._stopped or self._task:
            return
        if not self._db.path.exists():
            self.stop()
            return
        self._job = self._db.sentence_check_claim(self._owner, local=self._local)
        if self._job is None:
            return
        presentation_id, engine = self._job["id"], self._engine

        def check(report):
            """Finalize ownership in the worker even after UI closure; e.g. a late response cannot strand its job in checking state."""

            def progress(message):
                """Keep the lease alive at provider milestones; e.g. independent vocabulary repair needs no second worker."""

                self._db.sentence_check_touch(presentation_id, self._owner)
                report(message)

            try:
                engine.check(presentation_id, progress)
            except task_cancel.CancelledError:
                self._db.sentence_check_done(presentation_id, self._owner, cancelled=True)
                raise
            except Exception as exc:
                self._db.sentence_check_done(presentation_id, self._owner, error=str(exc))
                raise
            else:
                self._db.sentence_check_done(presentation_id, self._owner)
            return presentation_id

        task = library_workers.BackgroundTask(check)
        self._task = task
        task.signals.finished.connect(self._done)
        task.signals.failed.connect(self._done)
        task.signals.cancelled.connect(self._done)
        self.changed.emit(presentation_id)
        QtCore.QThreadPool.globalInstance().start(task)

    def _done(self, *_args) -> None:
        """Notify only identity and reread authoritative state; e.g. reporting during HTTP wins over a late correction."""

        presentation_id = self._job["id"]
        self._task, self._job = None, None
        self.changed.emit(presentation_id)
        if not self._stopped:
            self._wake.start(0)

    def stop(self) -> None:
        """Leave pending work durable at application exit; e.g. no GUI shutdown waits for an HTTP timeout."""

        self._stopped = True
        self._wake.stop()
        if self._task:
            self._task.cancel()
