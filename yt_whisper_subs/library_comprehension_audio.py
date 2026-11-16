"""Play a concealed long-form passage through cached local speech fragments.

Example: normal and slow narration share paragraph navigation without placing transcript text in any label.
"""

from PySide6 import QtCore
from PySide6 import QtWidgets
import shiboken6

from yt_whisper_subs import comprehension as comp
from yt_whisper_subs import library_listening
from yt_whisper_subs import library_workers


class Narration(QtWidgets.QWidget):
    """Own sequential playback and repair, independent from question submission; e.g. answering a question never restarts the article."""

    def __init__(self, audio, *, player=None) -> None:
        """Use the existing nonblocking WAV player; e.g. no speech subprocess runs on the GUI thread."""

        super().__init__()
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred, QtWidgets.QSizePolicy.Policy.Maximum)
        self._audio = audio
        self._player = player or library_listening.WavePlayer(self)
        self._player.finished.connect(self._next)
        self._session = None
        self._opts = None
        self._rows = []
        self._index, self._speed, self._playing = 0, 'normal', False
        self._task = None
        self._task_session = None
        self._wanted = False
        self._note = QtWidgets.QLabel('Luister naar de tekst en beantwoord de vragen.')
        self._note.setWordWrap(True)
        self._note.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self._paragraph = QtWidgets.QComboBox()
        self._paragraph.setAccessibleName('Begin bij een alinea')
        self._paragraph.activated.connect(self._jump)
        self._paragraph.setToolTip('Kies een alinea zonder het transcript te tonen.')
        controls = QtWidgets.QGridLayout()
        actions = (('▶ Luisteren', lambda: self.play('normal')), ('▶ Langzamer', lambda: self.play('slow')),
                   ('Vanaf het begin', self.restart), ('Stop', self.stop))
        for idx, (label, fn) in enumerate(actions):
            button = QtWidgets.QPushButton(label)
            button.setAutoDefault(False)
            button.clicked.connect(fn)
            controls.addWidget(button, idx // 2, idx % 2)
        controls.addWidget(self._paragraph, 2, 0, 1, 2)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(controls)
        layout.addWidget(self._note)

    def load(self, session: dict) -> None:
        """Bind saved modality and narrator choices; e.g. revisiting a correction does not interrupt already playing speech."""

        if self._session and self._session['id'] == session['id']:
            return
        self.stop()
        if self._task:
            self._task.cancel()
        self._task, self._task_session = None, None
        self._session, self._index, self._rows = session, 0, []
        self._opts = comp.Options.model_validate_json(session['options_json'])
        try:
            self._rows = self._audio.rows(session['passage_id'], self._opts)
        except (ValueError, OSError):
            self._note.setText('Spraak ontbreekt. Klik op Luisteren om de fragmenten te herstellen.')
        self._paragraphs()

    def _paragraphs(self) -> None:
        """Refresh available paragraph jumps after loading or repair; e.g. deleted initial audio can recover its navigation too."""

        self._paragraph.clear()
        seen = set()
        for row in self._rows:
            if row['paragraph'] not in seen:
                self._paragraph.addItem(f"Alinea {row['paragraph']}", row['index'])
                seen.add(row['paragraph'])

    def play(self, speed='normal') -> None:
        """Start or resume one chosen speed; e.g. slow narration uses independently synthesized clips."""

        if not self._session or not self.isVisible():
            return
        self._speed, self._wanted = speed, True
        if self._task:
            return
        try:
            if not self._rows:
                self._repair()
                return
            row = self._rows[self._index]
            if not self._audio.valid(row, speed):
                self._rows = []
                self._repair()
                return
            path = row['paths'][speed]
            self._player.play(path, row[speed + '_seconds'])
            self._audio.played(self._session['id'], row, speed)
            self._playing = True
            self._note.setText(f"Alinea {row['paragraph']} · fragment {self._index + 1}/{len(self._rows)} · {'langzaam' if speed == 'slow' else 'normaal'}")
        except (ValueError, OSError, RuntimeError) as exc:
            self.stop()
            self._note.setText('Afspelen is niet gelukt: ' + str(exc))

    def _next(self) -> None:
        """Continue only owned visible narration; e.g. closing practice between fragments prevents late playback."""

        if not self._playing or not self.isVisible():
            return
        self._index += 1
        if self._index >= len(self._rows):
            self.stop()
            self._index = 0
            self._note.setText('De tekst is afgelopen. Je kunt opnieuw luisteren.')
        else:
            self.play(self._speed)

    def _jump(self, idx: int) -> None:
        """Replay a paragraph without revealing its words; e.g. revisit the second speaker's argument by ear."""

        self.stop()
        self._index = self._paragraph.itemData(idx) or 0
        self.play(self._speed)

    def restart(self) -> None:
        """Restart the complete passage deliberately; e.g. a fresh listen keeps the same stored questions."""

        self.stop()
        self._index = 0
        self.play(self._speed)

    def stop(self) -> None:
        """Silence only this player's recording; e.g. hiding the comprehension page never leaves speech playing."""

        self._player.stop()
        self._playing, self._wanted = False, False

    def _repair(self) -> None:
        """Recover cached media without generating another passage; e.g. deleting a WAV does not lose typed answers."""

        session_id = self._session['id']
        passage_id, opts = self._session['passage_id'], self._opts
        task = library_workers.BackgroundTask(lambda report: self._audio.prepare(passage_id, opts, report))
        self._task = task
        self._task_session = session_id
        task.signals.progress.connect(self._note.setText)
        task.signals.finished.connect(lambda _: self._repaired(session_id))
        task.signals.failed.connect(lambda message: self._failed(session_id, message))
        task.signals.cancelled.connect(lambda: self._failed(session_id, 'Spraakvoorbereiding gepauzeerd.'))
        QtCore.QThreadPool.globalInstance().start(task)

    def _repaired(self, session_id: str) -> None:
        """Use repaired audio only for the same visible passage; e.g. a late completion cannot speak over another practice mode."""

        if shiboken6.isValid(self) and self._task_session == session_id:
            try:
                self._rows = self._audio.rows(self._session['passage_id'], self._opts)
            except (ValueError, OSError) as exc:
                self._failed(session_id, str(exc))
                return
            self._task, self._task_session = None, None
            self._paragraphs()
            if self._wanted:
                self.play(self._speed)

    def _failed(self, session_id, message) -> None:
        """Show an operational error without changing the learner's score; e.g. a failed expressive narrator is not a comprehension mistake."""

        if shiboken6.isValid(self) and self._task_session == session_id:
            self._task, self._task_session = None, None
            self._note.setText('Spraak niet beschikbaar: ' + message)

    def hideEvent(self, event) -> None:
        """Stop playback and owned repair on navigation; e.g. hidden listening content cannot autoplay."""

        self.shutdown()
        super().hideEvent(event)

    def shutdown(self) -> None:
        """Cancel only owned media work as well as playback; e.g. deleting the practice window cannot leave a repair callback targeting its labels."""

        self.stop()
        if self._task:
            self._task.cancel()
