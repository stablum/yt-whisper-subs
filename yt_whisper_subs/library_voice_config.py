"""Edit and audition named listening voices in a native preset dialog.

Example: enable Piper and two Zonos characters, preview them, then switch instantly during practice.
"""

import hashlib
import uuid
import wave
from pathlib import Path

from PySide6 import QtCore
from PySide6 import QtGui
from PySide6 import QtWidgets

from yt_whisper_subs import library_voice_controls
from yt_whisper_subs import library_workers
from yt_whisper_subs import listening_tts
from yt_whisper_subs import listening_voices
from yt_whisper_subs import zonos_install


class VoiceDialog(QtWidgets.QDialog):
    """Keep named presets and their sound settings together; e.g. only checked voices are prepared for practice."""

    def __init__(self, db, parent=None) -> None:
        """Build an editable local copy without loading models; e.g. Cancel leaves the library's saved voice set untouched."""

        super().__init__(parent)
        self.finished.connect(self.deleteLater)
        from yt_whisper_subs import library_listening

        self.setWindowTitle("Listening voices")
        self.resize(1120, 780)
        self._db = db
        self._presets = listening_voices.read(db).presets
        self._index = -1
        self._task = None
        self._closing = False
        self._player = library_listening.WavePlayer(self)
        self._list = QtWidgets.QListWidget()
        self._list.setMinimumWidth(205)
        self._list.setMaximumWidth(260)
        self._list.currentRowChanged.connect(self._select)
        self._name = QtWidgets.QLineEdit()
        self._name.setMaxLength(60)
        self._name.setPlaceholderText("Preset name")
        self._name.editingFinished.connect(self._rename)
        self._editor = library_voice_controls.VoiceEditor()
        self._sample = QtWidgets.QLineEdit("Morgen fietsen we naar de winkel, omdat de nieuwe fietspaden veel veiliger zijn.")
        self._sample.setMaxLength(600)
        self._sample.setAccessibleName("Voice preview sentence")
        self._preview = QtWidgets.QPushButton("▶ Preview")
        self._preview.clicked.connect(self._audition)
        self._slow = QtWidgets.QPushButton("▶ Preview slowly")
        self._slow.clicked.connect(lambda: self._audition(slow=True))
        stop = QtWidgets.QPushButton("Stop")
        stop.clicked.connect(self._player.stop)
        self._status = QtWidgets.QLabel()
        self._status.setWordWrap(True)
        self._status.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self._arrange(stop)
        for preset in self._presets:
            self._append(preset)
        self._list.setCurrentRow(0)
        self._installation_status()

    def _arrange(self, stop) -> None:
        """Keep preset actions and preview controls visible beside scrolling style groups; e.g. users never lose Save beneath dozens of sliders."""

        left = QtWidgets.QVBoxLayout()
        left.addWidget(QtWidgets.QLabel("Voices available during practice"))
        left.addWidget(self._list, 1)
        actions = QtWidgets.QGridLayout()
        for idx, (label, fn) in enumerate((("Add voice", self._add), ("Duplicate", self._duplicate), ("Remove", self._remove),
                                           ("Move up", lambda: self._move(-1)), ("Move down", lambda: self._move(1)))):
            button = QtWidgets.QPushButton(label)
            button.clicked.connect(fn)
            actions.addWidget(button, idx // 2, idx % 2)
        left.addLayout(actions)
        note = QtWidgets.QLabel("Check every voice you want to hear. Each enabled preset gets its own button on the same sentence. "
                               "The first enabled voice plays automatically.")
        note.setWordWrap(True)
        note.setMaximumWidth(250)
        left.addWidget(note)
        right = QtWidgets.QVBoxLayout()
        right.addWidget(self._name)
        right.addWidget(self._editor, 1)
        body = QtWidgets.QHBoxLayout()
        body.addLayout(left)
        body.addLayout(right, 1)
        preview = QtWidgets.QHBoxLayout()
        preview.addWidget(self._preview)
        preview.addWidget(self._slow)
        preview.addWidget(stop)
        preview.addStretch()
        self._install = QtWidgets.QPushButton("Install / repair Zonos")
        self._install.clicked.connect(self._setup)
        folder = QtWidgets.QPushButton("Open shared model folder")
        folder.clicked.connect(lambda: QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(zonos_install.root()))))
        preview.addWidget(folder)
        preview.addWidget(self._install)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Save | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(body, 1)
        layout.addWidget(QtWidgets.QLabel("Preview sentence · separate from your hidden practice question"))
        layout.addWidget(self._sample)
        layout.addLayout(preview)
        layout.addWidget(self._status)
        layout.addWidget(buttons)
        self.setStyleSheet("QLineEdit,QComboBox,QSpinBox,QDoubleSpinBox,QPushButton {font-size:11pt;} "
                           "QLabel {font-size:10pt;} QListWidget {font-size:12pt;} QLineEdit {padding:5px;}")

    def _append(self, preset) -> None:
        """Show enabled state independently from editor selection; e.g. an unchecked voice can still be previewed."""

        item = QtWidgets.QListWidgetItem(preset.name)
        item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(QtCore.Qt.CheckState.Checked if preset.enabled else QtCore.Qt.CheckState.Unchecked)
        self._list.addItem(item)

    def _store(self) -> None:
        """Flush the visible editor before switching rows; e.g. changing speaker identity never erases another preset's adjustments."""

        if 0 <= self._index < len(self._presets):
            preset = self._presets[self._index]
            preset.name = self._name.text().strip() or preset.name
            preset.config = self._editor.config()
            self._list.item(self._index).setText(preset.name)

    def _select(self, idx: int) -> None:
        """Load a selected preset without playing or preparing it; e.g. browsing voices does not consume practice."""

        self._store()
        self._index = idx
        if 0 <= idx < len(self._presets):
            self._name.setText(self._presets[idx].name)
            self._editor.load(self._presets[idx].config)

    def _rename(self) -> None:
        """Update the list label after editing; e.g. Clear Dutch remains recognizable on the practice voice buttons."""

        self._store()

    def _add(self) -> None:
        """Create a named neutral Zonos preset; e.g. a learner can build a new character without replacing Piper."""

        self._store()
        preset = listening_voices.Preset(name=f"Zonos voice {len(self._presets)}", config=listening_voices.Config(engine="zonos"))
        self._presets.append(preset)
        self._append(preset)
        self._list.setCurrentRow(len(self._presets) - 1)

    def _duplicate(self) -> None:
        """Copy all audible settings under a new identity; e.g. compare one speaker's neutral and lively delivery."""

        self._store()
        name = self._presets[self._index].name[:53] + " · copy"
        preset = self._presets[self._index].model_copy(deep=True, update={"id": uuid.uuid4().hex, "name": name})
        self._presets.append(preset)
        self._append(preset)
        self._list.setCurrentRow(len(self._presets) - 1)

    def _remove(self) -> None:
        """Remove only a preset definition, retaining recorded history; e.g. existing voice assistance events keep their original audio identity."""

        if len(self._presets) <= 1:
            return
        idx = self._index
        self._index = -1
        self._presets.pop(idx)
        with QtCore.QSignalBlocker(self._list):
            self._list.takeItem(idx)
            self._list.setCurrentRow(min(idx, len(self._presets) - 1))
        self._select(self._list.currentRow())

    def _move(self, delta: int) -> None:
        """Set playback order through direct controls; e.g. move a clear voice first for automatic question playback."""

        self._store()
        old, idx = self._index, self._index + delta
        if not 0 <= idx < len(self._presets):
            return
        self._index = -1
        self._presets.insert(idx, self._presets.pop(old))
        with QtCore.QSignalBlocker(self._list):
            item = self._list.takeItem(old)
            self._list.insertItem(idx, item)
            self._list.setCurrentRow(idx)
        self._select(idx)

    def _installation_status(self) -> None:
        """Report actual shared runtime readiness; e.g. model availability is independent from which presets are checked."""

        try:
            zonos_install.installation()
            self._status.setText("ZONOS3-Mini installed · shared user cache · local CPU runtime")
        except RuntimeError as exc:
            self._status.setText(str(exc))

    def _setup(self) -> None:
        """Install the explicitly selected optional engine off the GUI thread; e.g. progress stays visible while source builds and models download."""

        if self._task:
            return
        self._busy(True)
        self._task = library_workers.BackgroundTask(zonos_install.install)
        self._task.signals.progress.connect(self._status.setText)
        self._task.signals.finished.connect(self._installed)
        self._task.signals.failed.connect(self._failed)
        self._task.signals.cancelled.connect(self._cancelled)
        QtCore.QThreadPool.globalInstance().start(self._task)

    def _installed(self, _result) -> None:
        """Refresh only after verified setup completes; e.g. failed builds never show Installed."""

        self._busy(False)
        self._installation_status()

    def _audition(self, _checked=False, *, slow=False) -> None:
        """Render an independent preview from the visible settings; e.g. auditioning a voice cannot reveal the exercise answer."""

        if self._task:
            return
        text, cfg = self._sample.text().strip(), self._editor.config()
        if not text:
            return
        try:
            engine = listening_voices.speech(self._db, cfg)
        except (ValueError, OSError) as exc:
            self._status.setText(str(exc))
            return
        key = hashlib.sha256((engine.profile + "\0" + text).encode("utf-8")).hexdigest()
        speed = "slow" if slow else "normal"
        path = zonos_install.root() / "previews" / f"{key}-{speed}.wav"

        def preview(report):
            """Reuse exact public preview audio; e.g. returning to the same slider settings spends no synthesis work."""

            try:
                listening_tts.duration(path)
            except (OSError, EOFError, ValueError, wave.Error):
                engine.render_many([(text, {speed: path})], report)
            return {"path": path, "seconds": listening_tts.duration(path), "identity": cfg.identity(), "text": text}

        self._player.stop()
        self._busy(True)
        self._status.setText("Preparing voice preview…")
        self._task = library_workers.BackgroundTask(preview)
        self._task.signals.progress.connect(self._status.setText)
        self._task.signals.finished.connect(self._previewed)
        self._task.signals.failed.connect(self._failed)
        self._task.signals.cancelled.connect(self._cancelled)
        QtCore.QThreadPool.globalInstance().start(self._task)

    def _previewed(self, result: dict) -> None:
        """Play only a preview that still matches the editor; e.g. an old take cannot be mistaken for newly adjusted settings."""

        if self._closing:
            return
        self._busy(False)
        try:
            current = self._editor.config().identity()
        except (ValueError, OSError):
            current = None
        if current != result["identity"] or self._sample.text().strip() != result["text"]:
            self._status.setText("Settings changed. Preview again to hear the new version.")
            return
        self._player.play(result["path"], result["seconds"])
        self._status.setText("Playing preview · practice questions and statistics are unchanged")

    def _busy(self, value: bool) -> None:
        """Bound work to one owned task while keeping sliders responsive; e.g. repeated Preview clicks cannot queue many model runs."""

        if not value:
            self._task = None
        for button in (self._preview, self._slow, self._install):
            button.setEnabled(not value)

    def _failed(self, message: str, _trace="") -> None:
        """Keep failed previews recoverable without changing saved presets; e.g. a missing reference recording can be replaced immediately."""

        self._busy(False)
        self._status.setText(message)

    def _cancelled(self) -> None:
        """Leave completed cached files reusable after cancellation; e.g. closing setup cannot discard verified model downloads."""

        self._busy(False)

    def _save(self) -> None:
        """Persist the enabled comparison set without ending the current round; e.g. new presets warm existing sentences rather than generating replacements."""

        self._store()
        for idx, preset in enumerate(self._presets):
            preset.enabled = self._list.item(idx).checkState() == QtCore.Qt.CheckState.Checked
        try:
            voices = listening_voices.Voices(presets=self._presets)
            for row in voices.enabled:
                listening_voices.speech(self._db, row.config)  # Validate local reference identity before committing.
                if row.config.engine == "zonos":
                    zonos_install.installation()
            self._db.set_setting(listening_voices.SETTING, voices.model_dump_json())
        except (ValueError, OSError, RuntimeError) as exc:
            self._status.setText(str(exc))
            return
        self._close_work()
        self.accept()

    def _close_work(self) -> None:
        """Stop owned preview/install work at dialog closure; e.g. no model process survives a cancelled configuration window."""

        self._closing = True
        self._player.stop()
        if self._task:
            self._task.cancel()

    def reject(self) -> None:
        """Discard unsaved definitions while preserving external installation results; e.g. Cancel does not change enabled voices."""

        self._close_work()
        super().reject()
