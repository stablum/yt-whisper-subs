"""Play a concealed long-form passage through cached local speech fragments.

Example: normal and slow narration share paragraph navigation without placing transcript text in any label.
"""

import re

from PySide6 import QtCore
from PySide6 import QtWidgets
import shiboken6

from yt_whisper_subs import i18n
from yt_whisper_subs import comprehension as comp
from yt_whisper_subs import library_listening
from yt_whisper_subs import library_workers
from yt_whisper_subs import listening_voices


class Narration(QtWidgets.QWidget):
    """Own sequential playback and repair, independent from question submission; e.g. answering a question never restarts the article."""

    def __init__(self, db, audio, *, player=None) -> None:
        """Use the existing nonblocking WAV player; e.g. no speech subprocess runs on the GUI thread."""

        super().__init__()
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred, QtWidgets.QSizePolicy.Policy.Maximum)
        self._db, self._audio = db, audio
        self._player = player or library_listening.WavePlayer(self)
        self._player.finished.connect(self._next)
        self._session = None
        self._opts = None
        self._rows = []
        self._index, self._speed, self._playing = 0, 'normal', False
        self._task = None
        self._wanted = False
        self._voice = QtWidgets.QComboBox()
        self._voice.setAccessibleName(i18n.tr('Voice for this listening text'))
        self._voice.activated.connect(lambda _: self._select_voice(self._voice.currentData()))
        configure = QtWidgets.QPushButton(i18n.tr('Configure voices…'))
        configure.setAutoDefault(False)
        configure.clicked.connect(self._configure_voices)
        voices = QtWidgets.QFormLayout()
        voices.addRow(i18n.tr('Voice'), self._voice)
        voices.addRow(configure)
        self._note = QtWidgets.QLabel(i18n.tr('Listen to the text and answer the questions.'))
        self._note.setWordWrap(True)
        self._note.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self._paragraph = QtWidgets.QComboBox()
        self._paragraph.setAccessibleName(i18n.tr('Start at a paragraph'))
        self._paragraph.activated.connect(self._jump)
        self._paragraph.setToolTip(i18n.tr('Choose a paragraph without revealing the transcript.'))
        controls = QtWidgets.QGridLayout()
        actions = ((i18n.tr('▶ Listen'), lambda: self.play('normal')), (i18n.tr('▶ Slower'), lambda: self.play('slow')),
                   (i18n.tr('From the beginning'), self.restart), (i18n.tr('Stop'), self.stop))
        for idx, (label, fn) in enumerate(actions):
            button = QtWidgets.QPushButton(label)
            button.setAutoDefault(False)
            button.clicked.connect(fn)
            controls.addWidget(button, idx // 2, idx % 2)
        controls.addWidget(self._paragraph, 2, 0, 1, 2)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(voices)
        layout.addLayout(controls)
        layout.addWidget(self._note)

    def load(self, session: dict, *, refresh=False) -> None:
        """Bind saved modality and narrator choices; e.g. revisiting a correction does not interrupt already playing speech."""

        same = self._session is not None and self._session['id'] == session['id']
        opts = comp.Options.model_validate_json(session['options_json'])
        if same and self._opts.voice_id == opts.voice_id and not refresh:
            return
        self.shutdown()
        self._session, self._rows, self._opts = session, [], opts
        if not same:
            self._index = 0
        self._voices()
        self._load_recordings()
        self._warm()

    def _load_recordings(self) -> None:
        """Refresh the selected voice from disk; e.g. a background fill can make an open passage ready without user input."""

        try:
            self._rows = self._audio.rows(self._session['passage_id'], self._opts)
            self._note.setText(i18n.tr('Voice ready: {value}. Choose Listen to play.', value=self._voice.currentText()))
        except (ValueError, OSError, RuntimeError):
            self._rows = []
        self._paragraphs()

    def _warm(self) -> None:
        """Start missing narration at entry without requesting playback; e.g. Play can join a render already underway."""

        if self.isVisible() and self._session and not self._rows and not self._task:
            self._repair()

    def _voices(self) -> None:
        """Refresh shared presets without emitting an accidental selection; e.g. renaming a voice does not reset the passage."""

        self._voice.clear()
        for preset in listening_voices.read(self._db).presets:
            if preset.enabled or preset.id == self._opts.voice_id:
                self._voice.addItem(preset.name, preset.id)
        self._voice.setCurrentIndex(self._voice.findData(self._opts.voice_id))

    def _select_voice(self, voice_id: str) -> None:
        """Apply a voice immediately to the same text and remember it; e.g. comparing Piper and Zonos preserves the typed answer and paragraph."""

        if not self._session or not voice_id:
            return
        speed = self._speed
        self.shutdown()
        try:
            opts = self._opts.model_copy(update=dict(voice_id=voice_id))
            self._audio.preset(opts)
            session = self._db.comprehension_voice(self._session['id'], voice_id)
            self.load(session, refresh=True)
            self.play(speed)
        except (ValueError, OSError) as exc:
            self._voices()
            self._note.setText(i18n.tr('Could not change voice: ') + str(exc))

    def _configure_voices(self) -> None:
        """Edit the existing sound presets where they are played; e.g. saving changed Zonos sliders refreshes the same voice ID's recording."""

        from yt_whisper_subs import library_voice_config

        self.stop()
        if library_voice_config.VoiceDialog(self._db, self).exec() == QtWidgets.QDialog.DialogCode.Accepted:
            voices = listening_voices.read(self._db)
            selected = self._opts.voice_id
            if not any(p.id == selected for p in voices.presets):
                selected = voices.enabled[0].id
            self._select_voice(selected)

    def _paragraphs(self) -> None:
        """Refresh available paragraph jumps after loading or repair; e.g. deleted initial audio can recover its navigation too."""

        self._paragraph.clear()
        seen = set()
        for row in self._rows:
            if row['paragraph'] not in seen:
                self._paragraph.addItem(i18n.tr('Paragraph {paragraph}', paragraph=row['paragraph']), row['index'])
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
            self._note.setText(i18n.tr(
                '{value} · paragraph {paragraph} · clip {value3}/{len} · {value5}',
                value=self._voice.currentText(),
                paragraph=row['paragraph'],
                value3=self._index + 1,
                len=len(self._rows),
                value5=i18n.tr('slow') if speed == 'slow' else i18n.tr('normal'),
            ))
        except (ValueError, OSError, RuntimeError) as exc:
            self.stop()
            self._note.setText(i18n.tr('Could not play: ') + str(exc))

    def _next(self) -> None:
        """Continue only owned visible narration; e.g. closing practice between fragments prevents late playback."""

        if not self._playing or not self.isVisible():
            return
        self._index += 1
        if self._index >= len(self._rows):
            self.stop()
            self._index = 0
            self._note.setText(i18n.tr('The text has ended. You can listen again.'))
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

        passage_id, opts = self._session['passage_id'], self._opts
        def prepare(report):
            """Join cache preparation in a worker and return verified recordings for playback."""

            self._audio.prepare(passage_id, opts, report)
            return self._audio.rows(passage_id, opts)

        task = library_workers.BackgroundTask(prepare)
        self._task = task
        self._note.setText(i18n.tr('Preparing voice: {value}…', value=self._voice.currentText()))
        task.signals.progress.connect(lambda message: self._progress(task, message))
        task.signals.finished.connect(lambda rows: self._repaired(task, rows))
        task.signals.failed.connect(lambda message: self._failed(task, message))
        task.signals.cancelled.connect(lambda: self._failed(task, i18n.tr('Speech preparation paused.')))
        QtCore.QThreadPool.globalInstance().start(task)

    def _progress(self, task, message) -> None:
        """Ignore obsolete voice progress as well as obsolete results; e.g. an old Zonos task cannot overwrite Piper's playback label."""

        if shiboken6.isValid(self) and self._task is task:
            count = re.search(r'(\d+/\d+)$', message.strip())
            progress = i18n.tr(' · clip ') + count.group(1) if count else '…'
            self._note.setText(i18n.tr('Preparing voice: {value}{progress}', value=self._voice.currentText(), progress=progress))

    def _repaired(self, task, rows) -> None:
        """Use repaired audio only for the same visible passage; e.g. a late completion cannot speak over another practice mode."""

        if shiboken6.isValid(self) and self._task is task:
            self._rows = rows
            self._task = None
            self._paragraphs()
            self._note.setText(i18n.tr('Voice ready: {value}. Choose Listen to play.', value=self._voice.currentText()))
            if self._wanted:
                self.play(self._speed)

    def _failed(self, task, message) -> None:
        """Show an operational error without changing the learner's score; e.g. a failed expressive narrator is not a comprehension mistake."""

        if shiboken6.isValid(self) and self._task is task:
            self._task = None
            self._note.setText(i18n.tr('Speech unavailable: ') + message)

    def hideEvent(self, event) -> None:
        """Stop playback and owned repair on navigation; e.g. hidden listening content cannot autoplay."""

        self.shutdown()
        super().hideEvent(event)

    def showEvent(self, event) -> None:
        """Resume preparation after navigation; e.g. a cancelled warm-up restarts when the listening pane returns."""

        super().showEvent(event)
        QtCore.QTimer.singleShot(0, self, self._warm)

    def shutdown(self) -> None:
        """Cancel only owned media work as well as playback; e.g. deleting the practice window cannot leave a repair callback targeting its labels."""

        self.stop()
        if self._task:
            self._task.cancel()
            self._task = None
