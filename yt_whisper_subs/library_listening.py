"""Native listening controls with a concealed script and reusable local audio.

Example: listen, replay slowly if needed, type Dutch, then inspect Dutch corrections and English meaning.
"""

import json

from PySide6 import QtCore
from PySide6 import QtWidgets
import shiboken6

from yt_whisper_subs import library_sentences
from yt_whisper_subs import library_workers
from yt_whisper_subs import library_written_controls as written
from yt_whisper_subs import listening_tts
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

    def __init__(self, db, *, player=None) -> None:
        """Attach lightweight playback controls to the shared card; e.g. native tests inject a silent player."""

        super().__init__()
        self._db = db
        self._player = player or WavePlayer(self)
        self._row = None
        self._task = None
        self._replay = QtWidgets.QPushButton("Listen again · Alt+P")
        self._replay.setShortcut("Alt+P")
        self._slow = QtWidgets.QPushButton("Listen slowly · Alt+L")
        self._slow.setShortcut("Alt+L")
        self._stop = QtWidgets.QPushButton("Stop")
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

        self._stopped()
        self._row = None
        self._controls.hide()
        self._audio_note.hide()
        super().idle(title, message)

    def present(self, row: dict) -> None:
        """Show a listening task without placing Dutch or English in any label; e.g. pending feedback cannot leak the answer."""

        changed = self._row is None or self._row["id"] != row["id"]
        if changed:
            self._stopped()
        self._row = row
        self.editor.setPlaceholderText("Type the Dutch sentence you hear…")
        self.editor.setAccessibleName("Your Dutch transcription")
        self.show_question(written.Question(source="Listen and type what you hear",
                                            hint=f"Dutch dictation  ·  {sentences.SCENES[row['scene']]}",
                                            instruction="Write the complete Dutch sentence. You can listen again or slow it down.",
                                            draft=row["answer_text"]))
        self._controls.show()
        self._audio_note.show()
        self._audio_note.setText("English meaning appears with the correction.")
        if changed and row["answer_text"] is None and row["outcome"] is None:
            QtCore.QTimer.singleShot(0, self, lambda: self._autoplay(row["id"]))

    def _autoplay(self, presentation_id: int) -> None:
        """Avoid late playback after navigation or submission; e.g. a scheduled first listen cannot speak behind Activity."""

        if self.isVisible() and not self.window().isMinimized() and self._row and self._row["id"] == presentation_id and self._row["answer_text"] is None:
            self._play("normal")

    def _play(self, speed: str) -> None:
        """Replay cached speech and persist assistance separately; e.g. three listens remain one submitted practice answer."""

        if not self._row or not self.isVisible():
            return
        try:
            audio = self._db.listening_audio(self._row["question_id"])
            if not audio or not self._db.listening_audio_valid(audio):
                self._repair_audio()
                return
            path = self._db.listening_paths(audio["cache_key"])[speed]
            self._player.play(path, audio[f"{speed}_seconds"])
            self._db.listening_play(self._row["id"], speed)
            self._stop.setEnabled(True)
            self._audio_note.setText("Playing slowly…" if speed == "slow" else "Playing…")
        except (OSError, ValueError) as exc:
            self._audio_note.setText(f"Could not play audio: {exc}")

    def _repair_audio(self) -> None:
        """Rebuild missing speech off the GUI thread without changing drafts; e.g. clearing a cache is recoverable inside an active round."""

        if self._task:
            return
        row = self._row
        question = {"id": row["question_id"], "body_json": json.dumps(row["content"])}
        self._audio_note.setText("Preparing audio…")
        task = library_workers.BackgroundTask(lambda report: listening_tts.AudioCache(self._db).prepare([question], report))
        self._task = task
        task.signals.finished.connect(lambda _: self._repaired(row["id"]))
        task.signals.failed.connect(lambda message: self._repair_failed(row["id"], message))
        task.signals.cancelled.connect(lambda: self._repair_failed(row["id"], "Audio preparation paused."))
        QtCore.QThreadPool.globalInstance().start(task)

    def _repaired(self, presentation_id: int) -> None:
        """Replay only the repaired question still on screen; e.g. a late completion cannot speak the previous exercise."""

        self._task = None
        if shiboken6.isValid(self) and self._row and self._row["id"] == presentation_id:
            self._play("normal")

    def _repair_failed(self, presentation_id: int, message: str) -> None:
        """Offer a local retry without losing typed text; e.g. a voice download failure is not a wrong answer."""

        self._task = None
        if shiboken6.isValid(self) and self._row and self._row["id"] == presentation_id:
            self._audio_note.setText("Could not prepare audio. Click Listen again to retry. " + message.rsplit("\n", 1)[-1])

    def _stopped(self) -> None:
        """Stop only owned playback; e.g. switching sentences cannot leave overlapping voices."""

        self._player.stop()
        self._stop.setEnabled(False)

    def _playback_finished(self) -> None:
        """Keep playback status truthful after the clip ends; e.g. the learner can keep typing without a stale Playing indicator."""

        self._stop.setEnabled(False)
        self._audio_note.setText("Listen again if you need to.")

    def feedback(self, row: dict) -> None:
        """Reveal the transcript and English only in feedback; e.g. word mismatches retain the existing hover corrections."""

        super().feedback(row)
        self._source.setText("The sentence you heard")
        self._audio_note.setText("Listen again while comparing your transcription.")

    def hideEvent(self, event) -> None:
        """Silence hidden questions; e.g. closing the practice dialog never leaves speech playing."""

        self._stopped()
        if self._task:
            self._task.cancel()
        super().hideEvent(event)
