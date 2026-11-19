"""Read visible Dutch practice text without blocking input or revealing answers."""

from collections import deque

from PySide6 import QtCore
from PySide6 import QtWidgets
import shiboken6

from yt_whisper_subs import i18n
from yt_whisper_subs import library_listening
from yt_whisper_subs import library_workers
from yt_whisper_subs import pronunciation


SETTING = 'practice_auto_read_dutch'


class Controls(QtWidgets.QWidget):
    """Share one player across the visible modes, e.g. leaving a tab cancels its optional speech."""

    def __init__(self, db, *, audio=None, player=None) -> None:
        """Restore the saved autoplay preference and leave manual replay available."""

        super().__init__()
        self._db = db
        self._audio = audio or pronunciation.Audio(db)
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
        if cue and self.isVisible() and self._auto.isChecked():
            self._wake.start(0)

    def _toggle(self, enabled: bool) -> None:
        """Apply one durable preference across all pronunciation-enabled modes."""

        self._db.set_setting(SETTING, int(enabled))
        if enabled:
            self._repeat()
        else:
            self.stop()

    def _repeat(self) -> None:
        """Replay the current visible prompt or correction without changing practice progress."""

        if self._prompt:
            self.read([self._prompt[1]])

    def voice_changed(self) -> None:
        """Replay the visible request with the new preset, e.g. an article keeps using the chosen voice."""

        reading = self._reading
        self.stop()
        if reading:
            self.read(reading)
        elif self._auto.isChecked() and self.isVisible():
            self._repeat()

    def read(self, paragraphs: list[str]) -> None:
        """Prepare only the requested text off the GUI thread, e.g. read a visible article on demand."""

        if not self.isVisible() or self.window().isMinimized() or not paragraphs:
            return
        self.stop()
        self._reading = paragraphs
        self._stop.setEnabled(True)
        self._status.setText(i18n.tr('Preparing Dutch speech…'))
        task = library_workers.BackgroundTask(lambda report: self._audio.prepare(paragraphs, report))
        self._task = task
        task.signals.finished.connect(lambda clips: self._prepared(task, clips))
        task.signals.failed.connect(lambda message, _trace: self._failed(task, message))
        QtCore.QThreadPool.globalInstance().start(task)

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
        super().hideEvent(event)

    def showEvent(self, event) -> None:
        """Start a visible prompt after reopening the window, e.g. when Practice was opened from the tray."""

        super().showEvent(event)
        if self._prompt and self._auto.isChecked():
            self._wake.start(0)
