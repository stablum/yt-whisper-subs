"""Maintain a bounded standby round for each top-level practice destination.

Example: the next writing round is prepared while the current round is being answered.
"""

from __future__ import annotations

import functools
import time

from PySide6 import QtCore

from yt_whisper_subs import library_workers
from yt_whisper_subs import practice_tracks
from yt_whisper_subs import task_cancel


class PracticeReady(QtCore.QObject):
    """Prepare two destinations concurrently with per-track ownership and backoff; e.g. slow verb generation cannot block both sentence directions."""

    changed = QtCore.Signal()

    def __init__(self, db, parent=None, *, modes=None) -> None:
        """Start only at the application entry point; e.g. opening statistics never initiates paid work."""

        super().__init__(parent)
        self._db, self._modes = db, modes
        self._custom = modes is not None
        self._tasks = {}
        self._stopped = False
        self._pool = QtCore.QThreadPool(self)
        self._pool.setMaxThreadCount(2)
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(5000)
        self._timer.timeout.connect(self.check)
        self._timer.start()
        QtCore.QTimer.singleShot(1000, self, self.check)

    def _requests(self) -> list[tuple]:
        """Resolve each destination's own preferences; e.g. reading and writing lengths can differ without hiding a direction in an options menu."""

        if self._modes is None:
            from yt_whisper_subs import practice
            from yt_whisper_subs import practice_service
            from yt_whisper_subs import verb_practice
            from yt_whisper_subs import verb_service
            from yt_whisper_subs import sentence_practice
            from yt_whisper_subs import sentence_service

            self._modes = {"practice": (practice.RoundOptions(direction=practice.Direction.NL_EN, count=10), practice_service.PracticeService(self._db)),
                           "verb": (verb_practice.Options(), verb_service.VerbService(self._db)),
                           "sentence": (sentence_practice.Options(), sentence_service.SentenceService(self._db))}
        requests = []
        for track in practice_tracks.Track:
            if track.kind not in self._modes:
                continue
            defaults, engine = self._modes[track.kind]
            if self._custom and track.direction and track.direction != defaults.direction:
                continue
            saved = self._db.setting(track.settings_key, self._db.setting(track.kind + "_options", ""))
            try:
                opts = type(defaults).model_validate_json(saved) if saved else defaults
            except ValueError:
                opts = defaults
            if track.direction:
                opts = opts.model_copy(update={"direction": track.direction})
            requests.append((track, opts, engine))
        preferred = self._db.setting("practice_last_track", "")
        return sorted(requests, key=lambda request: request[0].value != preferred)

    def _needed(self, track, opts, engine) -> bool:
        """Keep one spare once practice is underway; e.g. unopened paid rounds are consumed before another is prepared."""

        status = self._db.practice_prepare_status(track.value)
        if max(status["next_at"], status["lease_until"]) > time.time():
            return False
        policy = {"model": engine._provider.model, "reasoning": engine._provider.reasoning,
                  "prompt_version": engine.start.prompt_version}
        if self._db.practice_stock(track, opts, policy) or self._db.practice_ready(track.kind, opts, policy):
            return False
        active = getattr(self._db, track.kind + "_active")
        session = active(track.direction.value) if track.direction else active()
        if session:
            summary = getattr(self._db, track.kind + "_summary")(session)
            answered = summary.get("submitted", summary["correct"] + summary["wrong"])
            if not answered:
                return False
        return True

    def _prepare_track(self, track, opts, engine, report) -> None:
        """Generate only a missing bounded spare; e.g. failures retain approved question stock and their per-direction cooldown."""

        report("Preparing " + track.label.replace("\n", " · "))
        try:
            engine.start(opts, report, cached_first=True, background=True)
        except Exception:
            if control := task_cancel.current():
                control.checkpoint()

    def prepare(self, report) -> None:
        """Fill eligible destinations synchronously for deterministic checks; e.g. fake providers still exercise real leases and persistence."""

        if not self._db.practice_has_candidates() and not self._db.verb_has_history():
            return
        for track, opts, engine in self._requests():
            if self._needed(track, opts, engine):
                self._prepare_track(track, opts, engine, report)

    def check(self) -> None:
        """Dispatch only available worker slots, prioritizing the selected destination; e.g. no unbounded job queue builds up behind slow API calls."""

        if self._stopped or len(self._tasks) >= 2:
            return
        if not self._db.practice_has_candidates() and not self._db.verb_has_history():
            return
        for track, opts, engine in self._requests():
            if track in self._tasks or not self._needed(track, opts, engine):
                continue
            task = library_workers.BackgroundTask(functools.partial(self._prepare_track, track, opts, engine))
            self._tasks[track] = task
            for signal in (task.signals.finished, task.signals.failed, task.signals.cancelled):
                signal.connect(functools.partial(self._finished, track))
            self._pool.start(task)
            if len(self._tasks) == 2:
                break

    def _finished(self, track, *_args) -> None:
        """Immediately refill free capacity rather than waiting another minute; e.g. one ready direction lets the next destination start promptly."""

        self._tasks.pop(track, None)
        self.changed.emit()
        if not self._stopped:
            QtCore.QTimer.singleShot(0, self, self.check)

    def stop(self) -> None:
        """Cancel owned work cooperatively without blocking exit; e.g. already cached approvals remain available after restart."""

        self._stopped = True
        self._timer.stop()
        for task in self._tasks.values():
            task.cancel()
