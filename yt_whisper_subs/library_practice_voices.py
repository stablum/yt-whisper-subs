"""Choose and edit the voice shared by every Dutch practice mode."""

from PySide6 import QtCore
from PySide6 import QtWidgets

from yt_whisper_subs import i18n
from yt_whisper_subs import library_voice_config
from yt_whisper_subs import listening_voices


class VoiceBar(QtWidgets.QWidget):
    """Keep speech preferences at the Practice level, e.g. changing pages keeps the selected preset."""

    changed = QtCore.Signal()
    configuring = QtCore.Signal()

    def __init__(self, db) -> None:
        """Show all enabled presets beside the editor entry point."""

        super().__init__()
        self._db = db
        self._pick = QtWidgets.QComboBox()
        self._pick.setMinimumContentsLength(14)
        self._pick.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self._pick.activated.connect(self._select)
        label = QtWidgets.QLabel(i18n.tr('Voice'))
        label.setBuddy(self._pick)
        self._pick.setAccessibleName(label.text())
        self._configure = QtWidgets.QPushButton(i18n.tr('Configure voices…'))
        self._configure.setAutoDefault(False)
        self._configure.clicked.connect(self._edit)
        layout = QtWidgets.QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(label, 0, 0)
        layout.addWidget(self._pick, 0, 1)
        layout.addWidget(self._configure, 1, 0, 1, 2)
        self._load()

    def _load(self) -> None:
        """Refresh labels and fallbacks after editing, e.g. disabling the current voice selects an enabled one."""

        voices = listening_voices.read(self._db)
        self._pick.clear()
        for preset in voices.enabled:
            self._pick.addItem(preset.name, preset.id)
        self._pick.setCurrentIndex(self._pick.findData(voices.selected.id))
        self._pick.setToolTip(voices.selected.name)

    def _select(self, idx: int) -> None:
        """Publish one selection to all playback and background preparation."""

        preset_id = self._pick.itemData(idx)
        if preset_id and preset_id != listening_voices.read(self._db).selected.id:
            listening_voices.select(self._db, preset_id)
            self._pick.setToolTip(self._pick.itemText(idx))
            self.changed.emit()

    def _edit(self) -> None:
        """Pause practice audio so previews can be heard, then refresh the enabled presets."""

        self.configuring.emit()
        dialog = library_voice_config.VoiceDialog(self._db, self)
        if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            self._load()
            self.changed.emit()
        dialog.deleteLater()
