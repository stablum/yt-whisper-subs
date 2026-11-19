"""Native listening controls with a concealed script and reusable local audio.

Example: listen, replay slowly if needed, type Dutch, then inspect Dutch corrections and English meaning.
"""

import json

from PySide6 import QtCore
from PySide6 import QtWidgets
import shiboken6

from yt_whisper_subs import i18n
from yt_whisper_subs import library_sentences
from yt_whisper_subs import library_workers
from yt_whisper_subs import library_written_controls as written
from yt_whisper_subs import listening_tts
from yt_whisper_subs import listening_voices
from yt_whisper_subs import sentence_practice as sentences


class WavePlayer(QtCore.QObject):
    """Use Windows' asynchronous WAV player without a video window or extra media runtime; e.g. replay never blocks typing."""

    finished = QtCore.Signal()

    def __init__(self, parent=None) -> None:
        """Own only the current playback timer; e.g. switching practice pages stops speech immediately."""

        super().__init__(parent)
        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.finished)

    def play(self, path, seconds: float) -> None:
        """Start an already validated recording; e.g. no TTS or subprocess runs on the UI thread."""

        import winsound

        winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
        self._timer.start(round(seconds * 1000) + 100)

    def stop(self) -> None:
        """Release native playback on navigation; e.g. hidden listening questions never keep speaking over a word quiz."""

        import winsound

        if self._timer.isActive():
            winsound.PlaySound(None, 0)
            self._timer.stop()


class ListeningCard(library_sentences.SentenceCard):
    """Share draft, feedback and correction-hover behavior while concealing both texts; e.g. accessible labels cannot expose the script early."""

    def __init__(self, db, *, player=None, audio_cache=None) -> None:
        """Attach lightweight playback controls to the shared card; e.g. native tests inject a silent player."""

        super().__init__()
        self._db = db
        self._player = player or WavePlayer(self)
        self._row = None
        self._task = None
        self._cache = audio_cache or (lambda: listening_tts.AudioCache(db))
        self._preset = listening_voices.read(db).selected
        self._speed = "normal"
        self._requested = None
        self._replay = QtWidgets.QPushButton(i18n.tr('Listen again · Alt+P'))
        self._replay.setShortcut("Alt+P")
        self._slow = QtWidgets.QPushButton(i18n.tr('Listen slowly · Alt+L'))
        self._slow.setShortcut("Alt+L")
        self._stop = QtWidgets.QPushButton(i18n.tr('Stop'))
        self._audio_note = QtWidgets.QLabel()
        self._audio_note.setWordWrap(True)
        self._audio_note.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self._audio_note.setStyleSheet("font-size:12pt;color:#b8c7d9;")
        self._controls = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(self._controls)
        layout.setContentsMargins(0, 0, 0, 0)
        for button in (self._replay, self._slow, self._stop):
            button.setAutoDefault(False)
            button.setStyleSheet("font-size:14pt;padding:10px 18px;")
            layout.addWidget(button)
        layout.addStretch()
        self.layout().insertWidget(1, self._controls)
        self.layout().insertWidget(2, self._audio_note)
        self._replay.clicked.connect(lambda: self._play("normal"))
        self._slow.clicked.connect(lambda: self._play("slow"))
        self._stop.clicked.connect(self._stopped)
        self._player.finished.connect(self._playback_finished)

    def idle(self, title: str, message: str) -> None:
        """Clear audio identity as well as text on completion; e.g. a round summary cannot replay a stale question."""

        self.shutdown()
        self._row = None
        self._controls.hide()
        self._audio_note.hide()
        super().idle(title, message)

    def present(self, row: dict) -> None:
        """Show a listening task without placing Dutch or English in any label; e.g. pending feedback cannot leak the answer."""

        changed = self._row is None or self._row["id"] != row["id"]
        if changed:
            self.shutdown()
        self._row = row
        self._preset = listening_voices.read(self._db).selected
        self.editor.setPlaceholderText(i18n.tr('Type the Dutch sentence you hear…'))
        self.editor.setAccessibleName(i18n.tr('Your Dutch transcription'))
        self.show_question(written.Question(source=i18n.tr('Listen and type what you hear'),
                                            hint=i18n.tr('Dutch dictation  ·  {value}', value=i18n.tr(sentences.SCENES[row['scene']])),
                                            instruction=i18n.tr('Write the complete Dutch sentence. You can listen again or slow it down.'),
                                            draft=row["answer_text"]))
        self._controls.show()
        self._audio_note.show()
        self._audio_note.setText(i18n.tr('English meaning appears with the correction.'))
        if changed and row["answer_text"] is None and row["outcome"] is None:
            QtCore.QTimer.singleShot(0, self, lambda: self._autoplay(row["id"]))
        elif changed:
            try:
                if not self._recording():
                    self._repair_audio()
            except (OSError, ValueError) as exc:
                self._audio_note.setText(i18n.tr('Could not play audio: {exc}', exc=exc))

    def voice_changed(self) -> None:
        """Use the shared preset for the same sentence, e.g. switching voices preserves the draft."""

        preset = listening_voices.read(self._db).selected
        if self._preset != preset:
            self.shutdown()
            self._preset = preset
            self._play(self._speed)

    def _autoplay(self, presentation_id: int) -> None:
        """Avoid late playback after navigation or submission; e.g. a scheduled first listen cannot speak behind Activity."""

        if self.isVisible() and not self.window().isMinimized() and self._row and self._row["id"] == presentation_id and self._row["answer_text"] is None:
            self._play("normal")

    def _play(self, speed: str) -> None:
        """Replay cached speech and persist assistance separately; e.g. three listens remain one submitted practice answer."""

        if not self._row or not self.isVisible():
            return
        try:
            self._speed = speed
            self._requested = self._row["id"]
            if not (audio := self._recording()):
                self._repair_audio()
                return
            path = self._db.listening_paths(audio["cache_key"])[speed]
            self._player.play(path, audio[f"{speed}_seconds"])
            self._db.listening_play(self._row["id"], speed, cache_key=audio["cache_key"])
            self._requested = None
            self._stop.setEnabled(True)
            self._audio_note.setText(i18n.tr('Playing slowly…') if speed == "slow" else i18n.tr('Playing…'))
        except (OSError, ValueError, RuntimeError) as exc:
            self._audio_note.setText(i18n.tr('Could not play audio: {exc}', exc=exc))

    def _recording(self) -> dict | None:
        """Resolve verified audio for the selected voice; e.g. a changed preset cannot play an old cache entry."""

        profile = self._cache().engine(self._preset.config).profile
        audio = self._db.listening_audio(self._row['question_id'], profile)
        return audio if audio and self._db.listening_audio_valid(audio) else None

    def _repair_audio(self) -> None:
        """Rebuild missing speech off the GUI thread without changing drafts; e.g. clearing a cache is recoverable inside an active round."""

        if self._task:
            return
        row = self._row
        preset = self._preset
        questions = [{"id": row["question_id"], "body_json": json.dumps(row["content"])}]
        self._audio_note.setText(i18n.tr('Preparing {name} for this sentence…', name=preset.name))
        task = library_workers.BackgroundTask(lambda report: self._cache().prepare(questions, report, preset=preset))
        self._task = task
        task.signals.finished.connect(lambda _: self._repaired(task))
        task.signals.failed.connect(lambda message, _: self._repair_failed(task, message))
        QtCore.QThreadPool.globalInstance().start(task)

    def _repaired(self, task) -> None:
        """Replay only the repaired question still on screen; e.g. a late completion cannot speak the previous exercise."""

        if shiboken6.isValid(self) and self._task is task:
            self._task = None
            self._audio_note.setText(i18n.tr('Listen again if you need to.'))
            if self._row and self._row["id"] == self._requested:
                self._play(self._speed)

    def _repair_failed(self, task, message: str) -> None:
        """Offer a local retry without losing typed text; e.g. a voice download failure is not a wrong answer."""

        if shiboken6.isValid(self) and self._task is task:
            self._task = None
            self._audio_note.setText(i18n.tr('Could not prepare audio. Click Listen again to retry. ') + message.rsplit("\n", 1)[-1])

    def _stopped(self) -> None:
        """Stop only owned playback; e.g. switching sentences cannot leave overlapping voices."""

        self._player.stop()
        self._requested = None
        self._stop.setEnabled(False)

    def _playback_finished(self) -> None:
        """Keep playback status truthful after the clip ends; e.g. the learner can keep typing without a stale Playing indicator."""

        self._stop.setEnabled(False)
        self._audio_note.setText(i18n.tr('Listen again if you need to.'))

    def feedback(self, row: dict) -> None:
        """Reveal the transcript and English only in feedback; e.g. word mismatches retain the existing hover corrections."""

        super().feedback(row)
        self._source.setText(i18n.tr('The sentence you heard'))
        self._audio_note.setText(i18n.tr('Listen again while comparing your transcription.'))

    def hideEvent(self, event) -> None:
        """Silence hidden questions; e.g. closing the practice dialog never leaves speech playing."""

        self.shutdown()
        super().hideEvent(event)

    def shutdown(self) -> None:
        """Cancel background repair and playback, e.g. the voice editor can preview privately."""

        self._stopped()
        if self._task:
            self._task.cancel()
            self._task = None
