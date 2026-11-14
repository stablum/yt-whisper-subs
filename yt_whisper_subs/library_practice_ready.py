"""Quiet native background preparation with durable, consumable practice stock.

Example: startup prepares one round per mode; an unanswered round survives restarts without further model calls.
"""

from __future__ import annotations

import time

from PySide6 import QtCore

from yt_whisper_subs import library_workers


class PracticeReady(QtCore.QObject):
    """Maintain one unconsumed round per mode; e.g. a pending correction prevents automatic replacement."""

    def __init__(self, db, parent=None, *, modes=None) -> None:
        """Start only from the real application entry point; e.g. constructing statistics widgets never makes paid requests."""

        super().__init__(parent)
        self._db, self._modes = db, modes
        self._task = None
        self._stopped = False
        self._pool = QtCore.QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(60000)
        self._timer.timeout.connect(self.check)
        self._timer.start()
        QtCore.QTimer.singleShot(3000, self, self.check)

    def check(self) -> None:
        """Run one quiet preparation worker; e.g. minute ticks cannot enqueue overlapping refill jobs."""

        if self._stopped or self._task:
            return
        task = library_workers.BackgroundTask(self.prepare)
        self._task = task
        task.signals.finished.connect(self._finished)
        task.signals.failed.connect(self._finished)
        task.signals.cancelled.connect(self._finished)
        self._pool.start(task)

    def prepare(self, report) -> None:
        """Fill only missing rounds using saved settings; e.g. opening the app repeatedly cannot accumulate a backlog."""

        if not self._db.practice_has_candidates() and not self._db.verb_has_history():
            return
        if self._modes is None:
            from yt_whisper_subs import practice
            from yt_whisper_subs import practice_service
            from yt_whisper_subs import verb_practice
            from yt_whisper_subs import verb_service
            from yt_whisper_subs import sentence_practice
            from yt_whisper_subs import sentence_service

            self._modes = {"practice": (practice.RoundOptions(direction=practice.Direction.NL_EN), practice_service.PracticeService(self._db)),
                           "verb": (verb_practice.Options(), verb_service.VerbService(self._db)),
                           "sentence": (sentence_practice.Options(), sentence_service.SentenceService(self._db))}
        preferred = {"vocabulary": "practice", "verbs": "verb", "sentences": "sentence"}.get(self._db.setting("practice_last_mode", "vocabulary"))
        kinds = sorted(self._modes, key=lambda kind: kind != preferred)
        for kind in kinds:
            defaults, engine = self._modes[kind]
            report(f"Checking ready {kind} round")
            status = self._db.practice_prepare_status(kind)
            if max(status["next_at"], status["lease_until"]) > time.time():
                continue
            if getattr(self._db, f"{kind}_active")() is not None:
                continue
            saved = self._db.setting(f"{kind}_options", "")
            try:
                opts = type(defaults).model_validate_json(saved) if saved else defaults
            except ValueError:
                opts = defaults
            try:
                engine.start(opts, report, cached_first=False, background=True)
            except Exception:
                # The shared preparation lease retains the error and one-hour cooldown; other modes can still become ready.
                from yt_whisper_subs import task_cancel

                if control := task_cancel.current():
                    control.checkpoint()

    def _finished(self, *_args) -> None:
        """Release only transient worker state; e.g. generated questions remain durable after the UI signal arrives."""

        self._task = None

    def stop(self) -> None:
        """Cancel cooperatively at app exit; e.g. completed approvals remain available after restart."""

        self._stopped = True
        self._timer.stop()
        if self._task:
            self._task.cancel()
