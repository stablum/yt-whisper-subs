"""Read visible Dutch practice text without blocking input or revealing answers."""

from collections import deque

from PySide6 import QtCore
from PySide6 import QtWidgets
import shiboken6

from yt_whisper_subs import i18n
from yt_whisper_subs import library_listening
from yt_whisper_subs import library_workers
from yt_whisper_subs import pronunciation
from yt_whisper_subs import pronunciation_stock


SETTING = 'practice_auto_read_dutch'


class RoundAudio(QtCore.QObject):
    """Prepare one round before exposure, independently of its playback requests."""

    progress = QtCore.Signal(str)
    finished = QtCore.Signal(str)

    def __init__(self, db, audio, parent) -> None:
        """Bind the same cache used by replay, e.g. warm standby clips require no synthesis here."""

        super().__init__(parent)
        self._db, self._audio = db, audio
        self._round = None
        self._task = None
        self._done = False
        self._resume = None

    @property
    def busy(self) -> bool:
        """Indicate that playback can join the preparation already underway."""

        return self._task is not None

    def bind(self, track, session) -> bool:
        """Switch rounds without allowing a stale callback to expose a hidden question."""

        key = (track, session) if session else None
        if key == self._round:
            return False
        self.invalidate()
        self._round, self._resume = key, None
        return True

    def invalidate(self) -> None:
        """Discard an obsolete render while keeping its already published cache entries."""

        if self._task:
            self._task.cancel()
            self._task = None
        self._done = False

    def ensure(self, resume=None) -> bool:
        """Return whether practice can proceed, otherwise resume after preparing the round."""

        if self._round is None or self._done:
            return True
        if resume is not None:
            self._resume = resume
        if self._task is None:
            track, session = self._round
            stock = pronunciation_stock.Stock(self._db, audio=self._audio())
            task = library_workers.BackgroundTask(lambda report: stock.prepare_session(track, session, report))
            self._task = task
            task.signals.finished.connect(lambda _: self._complete(task, ''))
            task.signals.failed.connect(lambda message, _: self._complete(task, message))
            task.signals.cancelled.connect(lambda: self._complete(task, ''))
            self.progress.emit(i18n.tr('Preparing Dutch speech…'))
            QtCore.QThreadPool.globalInstance().start(task, 1)
        return False

    def _complete(self, task, message: str) -> None:
        """Let optional audio errors leave text practice usable."""

        if shiboken6.isValid(self) and self._task is task:
            self._task, self._done = None, True
            self.resume()
            self.finished.emit(message)

    def resume(self) -> None:
        """Release the pending question, e.g. turning autoplay off starts the text exercise."""

        callback, self._resume = self._resume, None
        if callback:
            callback()


class Controls(QtWidgets.QWidget):
    """Share one player across the visible modes, e.g. leaving a tab cancels its optional speech."""

    round_opened = QtCore.Signal()

    def __init__(self, db, *, audio=None, player=None) -> None:
        """Restore the saved autoplay preference and leave manual replay available."""

        super().__init__()
        self._db = db
        self._audio = audio or pronunciation.Audio(db)
        self._round = RoundAudio(db, lambda: self._audio, self)
        self._player = player or library_listening.WavePlayer(self)
        self._player.finished.connect(self._next)
        self._prompt = None
        self._reading = None
        self._task = None
        self._clips = deque()
        self._wake = QtCore.QTimer(self)
        self._wake.setSingleShot(True)
        self._wake.timeout.connect(self._repeat)
        self._auto = QtWidgets.QCheckBox(i18n.tr('Auto-read Dutch'))
        self._auto.setChecked(db.setting(SETTING, '0') == '1')
        self._auto.toggled.connect(self._toggle)
        self._listen = QtWidgets.QPushButton(i18n.tr('Read aloud'))
        self._listen.setEnabled(False)
        self._listen.clicked.connect(self._repeat)
        self._stop = QtWidgets.QPushButton(i18n.tr('Stop'))
        self._stop.setEnabled(False)
        self._stop.clicked.connect(self.stop)
        self._status = QtWidgets.QLabel()
        self._status.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self._status.setWordWrap(True)
        self._round.progress.connect(self._status.setText)
        self._round.finished.connect(self._round_finished)
        actions = QtWidgets.QHBoxLayout()
        for button in (self._listen, self._stop):
            button.setAutoDefault(False)
            actions.addWidget(button)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._auto)
        layout.addLayout(actions)
        layout.addWidget(self._status)

    def prompt(self, key, text: str) -> None:
        """Speak once when the visible text changes, e.g. rerendering an old answer stays quiet."""

        cue = (key, text) if text else None
        if self._prompt == cue:
            return
        self.stop()
        self._prompt = cue
        self._listen.setEnabled(cue is not None)
        self._listen.setToolTip(text)
        if cue and self.isVisible() and self._auto.isChecked():
            self._wake.start(0)

    def _toggle(self, enabled: bool) -> None:
        """Apply one durable preference across all pronunciation-enabled modes."""

        self._db.set_setting(SETTING, int(enabled))
        if enabled:
            if not self._round.busy:
                self._round.invalidate()
            self._round.ensure()
            self._repeat()
        else:
            self.stop()
            self._round.resume()

    def bind_round(self, track, session) -> None:
        """Keep read-aloud tied to the visible round even if it already has an open question."""

        if self._round.bind(track, session) and session:
            self.round_opened.emit()

    def before_question(self, track, session, resume) -> bool:
        """With autoplay on, require both prompt and answer audio before a question appears."""

        self.bind_round(track, session)
        return not self._auto.isChecked() or self._round.ensure(resume)

    def _round_finished(self, message: str) -> None:
        """Play a queued request from cache once the selected round has prepared."""

        if self._reading:
            self.read(self._reading)
        elif message:
            self._status.setText(i18n.tr('Speech unavailable'))
            self._status.setToolTip(message)
        else:
            self._status.clear()

    def _repeat(self) -> None:
        """Replay the current visible prompt or correction without changing practice progress."""

        if self._prompt:
            self.read([self._prompt[1]])

    def voice_changed(self) -> None:
        """Replay the visible request with the new preset, e.g. an article keeps using the chosen voice."""

        reading = self._reading
        self.stop()
        self._round.invalidate()
        if reading or (self.isVisible() and self._auto.isChecked()):
            self._round.ensure()
        if reading:
            self.read(reading)
        elif self._auto.isChecked() and self.isVisible():
            self._repeat()

    def read(self, paragraphs: list[str]) -> None:
        """Queue playback of prepared clips, e.g. reviewing a correction never starts TTS."""

        if not self.isVisible() or self.window().isMinimized() or not paragraphs:
            return
        self.stop()
        self._reading = paragraphs
        self._stop.setEnabled(True)
        if self._round.busy:
            self._status.setText(i18n.tr('Preparing Dutch speech…'))
            return
        task = library_workers.BackgroundTask(lambda _: self._audio.cached(paragraphs))
        self._task = task
        task.signals.progress.connect(lambda message: self._progress(task, message))
        task.signals.finished.connect(lambda clips: self._prepared(task, clips))
        task.signals.failed.connect(lambda message, _trace: self._failed(task, message))
        task.signals.cancelled.connect(lambda: self._cancelled(task))
        QtCore.QThreadPool.globalInstance().start(task, 1)

    def _progress(self, task, message: str) -> None:
        """Show current synthesis progress without reviving a previous question's status."""

        if shiboken6.isValid(self) and self._task is task:
            if clips is None:
                self._failed(task, '')
                return
            self._status.setText(message)

    def _cancelled(self, task) -> None:
        """Clear the preparing indicator after a cancellation from the speech worker."""

        if shiboken6.isValid(self) and self._task is task:
            self.stop()

    def _prepared(self, task, clips) -> None:
        """Play only the latest result, e.g. a completed old render cannot speak over a new question."""

        if shiboken6.isValid(self) and self._task is task:
            self._task = None
            self._clips.extend(clips)
            self._status.clear()
            self._next()

    def _failed(self, task, message) -> None:
        """Keep a synthesis failure separate from practice grading and allow another manual attempt."""

        if shiboken6.isValid(self) and self._task is task:
            self.stop()
            self._status.setText(i18n.tr('Speech unavailable'))
            self._status.setToolTip(message)

    def _next(self) -> None:
        """Play consecutive clips while this page is still visible, e.g. a whole article reads in order."""

        if self._clips and self.isVisible() and not self.window().isMinimized():
            try:
                self._player.play(*self._clips.popleft())
                return
            except (OSError, RuntimeError):
                self.stop()
                self._status.setText(i18n.tr('Speech unavailable'))
                return
        self.stop()

    def stop(self) -> None:
        """Silence playback and cancel obsolete work, e.g. switching to dictation cannot play two voices."""

        self._wake.stop()
        self._reading = None
        if self._task:
            self._task.cancel()
            self._task = None
        self._clips.clear()
        self._player.stop()
        self._stop.setEnabled(False)
        self._status.clear()
        self._status.setToolTip('')

    def hideEvent(self, event) -> None:
        """Cancel speech when the practice window or controls are hidden."""

        self.stop()
        self.bind_round(None, None)
        super().hideEvent(event)

    def showEvent(self, event) -> None:
        """Start a visible prompt after reopening the window, e.g. when Practice was opened from the tray."""

        super().showEvent(event)
        if self._prompt and self._auto.isChecked():
            self._wake.start(0)
