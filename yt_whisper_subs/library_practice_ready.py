"""Maintain a bounded standby round for each top-level practice destination.

Example: the next writing round is prepared while the current round is being answered.
"""

from __future__ import annotations

import functools
import itertools
import logging
import time

from PySide6 import QtCore

from yt_whisper_subs import library_workers
from yt_whisper_subs import practice_ready
from yt_whisper_subs import practice_tracks
from yt_whisper_subs import pronunciation_stock
from yt_whisper_subs import task_cancel


class PracticeReady(QtCore.QObject):
    """Prepare two destinations concurrently with per-track ownership and backoff; e.g. slow verb generation cannot block both sentence directions."""

    changed = QtCore.Signal()
    _candidate = QtCore.Signal(object)

    def __init__(self, db, parent=None, *, modes=None, speech=None) -> None:
        """Start only at the application entry point; e.g. opening statistics never initiates paid work."""

        super().__init__(parent)
        self._db, self._modes = db, modes
        self._custom = modes is not None
        self._tasks = {}
        self._scan = None
        self._rescan = False
        self._stopped = False
        self._speech = pronunciation_stock.Stock(db) if speech is None and not self._custom else speech
        self._speech_task = None
        self._speech_retry = 0.0
        self._pool = QtCore.QThreadPool(self)
        self._pool.setMaxThreadCount(2)
        self._candidate.connect(self._schedule, QtCore.Qt.ConnectionType.QueuedConnection)
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
            from yt_whisper_subs import comprehension
            from yt_whisper_subs import comprehension_service

            self._modes = {"practice": (practice.RoundOptions(direction=practice.Direction.NL_EN, count=10), practice_service.PracticeService(self._db)),
                           "verb": (verb_practice.Options(), verb_service.VerbService(self._db)),
                           "sentence": (sentence_practice.Options(), sentence_service.SentenceService(self._db)),
                           'comprehension': (comprehension.Options(), comprehension_service.ComprehensionService(self._db))}
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
        if track.kind == 'comprehension':
            return engine.needs(opts)
        if track == practice_tracks.Track.LISTENING and engine.pending_dictation_audio():
            return True
        policy = practice_ready.question_policy(engine)
        stock = self._db.practice_stock(track, opts, policy)
        if stock:
            if track == practice_tracks.Track.LISTENING:
                return not engine.audio.ready(stock)
            return False
        if session := self._db.practice_ready(track.kind, opts, policy):
            if track == practice_tracks.Track.LISTENING:
                return not engine.audio.ready(self._db.listening_questions(session))
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
            if track == practice_tracks.Track.LISTENING and engine.prepare_resumed_dictation(report):
                return
            engine.start(opts, report, cached_first=True, background=True)
        except Exception:
            if control := task_cancel.current():
                control.checkpoint()

    def _pending(self, busy: frozenset = frozenset()):
        """Yield eligible requests in preferred order; e.g. an active listening check can inspect and hash cached recordings here."""

        if not self._db.practice_has_candidates() and not self._db.verb_has_history():
            return
        for track, opts, engine in self._requests():
            if control := task_cancel.current():
                control.checkpoint()
            if track not in busy and self._needed(track, opts, engine):
                yield track, opts, engine

    def prepare(self, report) -> None:
        """Fill eligible destinations synchronously for deterministic checks; e.g. fake providers still exercise real leases and persistence."""

        for track, opts, engine in self._pending():
            self._prepare_track(track, opts, engine, report)

    def check(self) -> None:
        """Queue readiness without touching the catalog on the UI thread; e.g. answering a quiz can repaint while WAVs are checked."""

        if self._stopped:
            return
        self._check_speech()
        capacity = self._pool.maxThreadCount() - len(self._tasks)
        if capacity <= 0:
            return
        if self._scan is not None:
            self._rescan = True
            return
        busy = frozenset(self._tasks)

        def scan(_report):
            """Send each eligible request as it is found; e.g. preferred word practice can start while audio is still checked."""

            for request in itertools.islice(self._pending(busy), capacity):
                self._candidate.emit(request)

        task = library_workers.BackgroundTask(scan)
        self._scan = task
        task.signals.finished.connect(self._end_scan)
        task.signals.cancelled.connect(self._end_scan)
        task.signals.failed.connect(self._scan_failed)
        self._pool.start(task)

    def _check_speech(self) -> None:
        """Keep one low-priority speech job outside the round-generation pool."""

        if self._stopped or self._speech is None or self._speech_task or time.monotonic() < self._speech_retry:
            return
        task = library_workers.BackgroundTask(lambda report: self._speech.warm(self._requests(), report))
        self._speech_task = task
        task.signals.finished.connect(self._speech_finished)
        task.signals.failed.connect(self._speech_failed)
        QtCore.QThreadPool.globalInstance().start(task, -1)

    @QtCore.Slot(object)
    def _speech_finished(self, warmed) -> None:
        """Recheck after a batch so new rounds can change the priority of the remaining clips."""

        self._speech_task = None
        if warmed and not self._stopped:
            QtCore.QTimer.singleShot(0, self, self._check_speech)

    @QtCore.Slot(str, str)
    def _speech_failed(self, message, _trace) -> None:
        """Let text practice continue during an audio problem and retry on a later tick."""

        self._speech_task = None
        self._speech_retry = time.monotonic() + 60
        logging.getLogger(__name__).warning('Practice speech preparation failed: %s', message)

    @QtCore.Slot(object)
    def _schedule(self, request) -> None:
        """Dispatch only while capacity remains; e.g. a queued candidate cannot start after shutdown."""

        track, opts, engine = request
        if self._stopped or track in self._tasks or len(self._tasks) >= self._pool.maxThreadCount():
            return
        task = library_workers.BackgroundTask(functools.partial(self._prepare_track, track, opts, engine))
        self._tasks[track] = task
        for signal in (task.signals.finished, task.signals.failed, task.signals.cancelled):
            signal.connect(functools.partial(self._finished, track), QtCore.Qt.ConnectionType.QueuedConnection)
        self._pool.start(task)

    @QtCore.Slot()
    def _end_scan(self) -> None:
        """Service one missed wake after a slow scan; e.g. rapid submissions never queue a scan per click."""

        self._scan = None
        again, self._rescan = self._rescan, False
        if again:
            self.check()

    @QtCore.Slot(str, str)
    def _scan_failed(self, message, _trace) -> None:
        """Release the scan slot on error so a transient catalog failure can retry on the next tick."""

        logging.getLogger(__name__).warning('Practice readiness check failed: %s', message)
        self._end_scan()

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
        if self._scan:
            self._scan.cancel()
        if self._speech_task:
            self._speech_task.cancel()
        for task in self._tasks.values():
            task.cancel()
