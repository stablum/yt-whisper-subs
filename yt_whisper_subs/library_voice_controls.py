"""Build native voice controls from the model's complete published direction catalogue.

Example: calibrated mood sliders and neutral reset buttons work identically for every named Zonos preset.
"""

import secrets

from PySide6 import QtCore
from PySide6 import QtWidgets

from yt_whisper_subs import listening_voices


class Strength(QtWidgets.QWidget):
    """Present one expressive axis in everyday terms; e.g. lower ↔ higher pitch around a neutral zero."""

    changed = QtCore.Signal()

    def __init__(self, definition: dict) -> None:
        """Keep a percentage slider and exact entry synchronized; e.g. 100% uses the published calibrated dose."""

        super().__init__()
        self.key = definition["id"]
        self._slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self._spin = QtWidgets.QSpinBox()
        minimum = -100 if definition["axis"] else 0
        for widget in (self._slider, self._spin):
            widget.setRange(minimum, 100)
        self._spin.setSuffix(" %")
        self._spin.setMinimumWidth(86)
        self._slider.setSingleStep(5)
        self._slider.setTickPosition(QtWidgets.QSlider.TickPosition.TicksBelow)
        self._slider.setTickInterval(50)
        self._slider.valueChanged.connect(self._spin.setValue)
        self._spin.valueChanged.connect(self._slider.setValue)
        self._spin.valueChanged.connect(self.changed)
        poles = f"{definition['negative']}  ←  neutral  →  {definition['positive']}" if minimum else "Neutral  →  stronger"
        title = QtWidgets.QLabel(definition["label"])
        title.setStyleSheet("font-weight:600")
        note = QtWidgets.QLabel(poles)
        note.setStyleSheet("color:#9cacc1;font-size:9pt")
        note.setWordWrap(True)
        reset = QtWidgets.QToolButton()
        reset.setText("↺")
        reset.setToolTip("Reset this control to neutral")
        reset.clicked.connect(lambda: self._spin.setValue(0))
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self._slider, 1)
        row.addWidget(self._spin)
        row.addWidget(reset)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 3, 0, 5)
        layout.setSpacing(1)
        layout.addWidget(title)
        layout.addWidget(note)
        layout.addLayout(row)

    def value(self) -> float:
        """Return normalized style strength; e.g. -0.5 means halfway toward the negative pole."""

        return self._spin.value() / 100

    def set_value(self, value: float) -> None:
        """Restore a saved strength without interpreting a voice label; e.g. duplicate presets reproduce all their sliders."""

        self._spin.setValue(round(value * 100))


class VoiceEditor(QtWidgets.QWidget):
    """Keep model settings editable without starting synthesis; e.g. experiment with sliders, then explicitly preview."""

    changed = QtCore.Signal()

    def __init__(self) -> None:
        """Group the complete catalogue into native pages; e.g. reference speaker identity stays separate from the Dutch exercise language."""

        super().__init__()
        self._engine = QtWidgets.QComboBox()
        self._engine.addItem("Piper · Dutch Ronnie", "piper")
        self._engine.addItem("ZONOS3-Mini", "zonos")
        self._voice = QtWidgets.QComboBox()
        self._voice.setAccessibleName("Reference speaker")
        self._voice.setToolTip("The listed language belongs to the reference recording. It does not change the exercise language.")
        for row in listening_voices.catalog()["voices"]:
            self._voice.addItem(f"{row['name']} · {row['language']} reference · {row['gender']} · {row['style']}", row["name"])
        self._reference = QtWidgets.QLineEdit()
        self._reference.setPlaceholderText("Optional local voice recording")
        self._reference.setClearButtonEnabled(True)
        self._reference.setToolTip("Use a short, clean recording of one speaker. Leave empty to use the selected built-in voice. Audio stays local.")
        browse = QtWidgets.QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        ref_row = QtWidgets.QHBoxLayout()
        ref_row.addWidget(self._reference, 1)
        ref_row.addWidget(browse)
        self._pace = self._number(60, 150, 100, " %")
        self._volume = self._number(20, 150, 100, " %")
        self._seed = QtWidgets.QSpinBox()
        self._seed.setRange(0, 2147483647)
        self._seed.setToolTip("The same variation number and settings reproduce the same take.")
        self._numbers = {"guidance": self._number(1, 3, 1), "headroom": self._number(0, 24, 3, " dB")}
        form = QtWidgets.QFormLayout()
        form.addRow("Speech engine", self._engine)
        self._zonos = QtWidgets.QWidget()
        details = QtWidgets.QFormLayout(self._zonos)
        details.setContentsMargins(0, 0, 0, 0)
        details.addRow("Reference speaker", self._voice)
        note = QtWidgets.QLabel("Listening sentences are Dutch. Zonos has no built-in Dutch reference speaker; "
                               "these speakers can speak Dutch, but their accent may vary. "
                               "Use a Dutch recording below as a reference, or choose Piper · Dutch Ronnie.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#b8c7d9;")
        details.addRow(note)
        details.addRow("Clone from recording", ref_row)
        form.addRow(self._zonos)
        form.addRow("Speaking pace", self._pace)
        form.addRow("Playback volume", self._volume)
        self._tabs = QtWidgets.QWidget()
        self._groups = QtWidgets.QListWidget()
        self._groups.setFixedWidth(190)
        self._groups.setWordWrap(True)
        self._groups.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._groups.setStyleSheet("QListWidget {font-size:10pt;padding:0;} QListWidget::item {padding:4px 6px;margin:0;}")
        self._pages = QtWidgets.QStackedWidget()
        self._groups.currentRowChanged.connect(self._pages.setCurrentIndex)
        styles = QtWidgets.QHBoxLayout(self._tabs)
        styles.setContentsMargins(0, 0, 0, 0)
        styles.addWidget(self._groups)
        styles.addWidget(self._pages, 1)
        self._strengths = {}
        self._arrange_styles()
        self._groups.setCurrentRow(0)
        self._quick = QtWidgets.QWidget()
        quick = QtWidgets.QHBoxLayout(self._quick)
        quick.setContentsMargins(0, 0, 0, 0)
        quick.addWidget(QtWidgets.QLabel("Starting style"))
        for name in ("Neutral", "Clear", "Conversational", "Lively", "Calm", "Whisper"):
            button = QtWidgets.QPushButton(name)
            button.setToolTip("Replace expression settings with this starting point; speaker identity stays the same.")
            button.clicked.connect(lambda _=False, style=name: self._style(style))
            quick.addWidget(button)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(form)
        layout.addWidget(self._quick)
        layout.addWidget(self._tabs, 1)
        self._engine.currentIndexChanged.connect(self._visibility)
        for widget in (self._engine, self._voice):
            widget.currentIndexChanged.connect(self.changed)
        self._reference.textChanged.connect(self.changed)
        self._reference.textChanged.connect(self._visibility)
        for widget in (self._pace, self._volume, self._seed, *self._numbers.values()):
            widget.valueChanged.connect(self.changed)
        self._visibility()

    @staticmethod
    def _number(low, high, value, suffix="") -> QtWidgets.QDoubleSpinBox:
        """Use consistent native numeric controls; e.g. seconds and percentages stay readable alongside precise values."""

        spin = QtWidgets.QDoubleSpinBox()
        spin.setRange(low, high)
        spin.setDecimals(0 if high > 30 else 2)
        spin.setValue(value)
        spin.setSingleStep(.05 if high <= 3 else 1)
        spin.setSuffix(suffix)
        return spin

    def _arrange_styles(self) -> None:
        """Expose every published direction with grouped resets; e.g. all 65 controls remain available without hand-maintained subsets."""

        groups = {}
        for definition in listening_voices.catalog()["controls"]:
            groups.setdefault(definition["group"], []).append(definition)
        for title, definitions in groups.items():
            page = QtWidgets.QWidget()
            layout = QtWidgets.QVBoxLayout(page)
            controls = []
            for definition in definitions:
                widget = Strength(definition)
                widget.changed.connect(self.changed)
                self._strengths[widget.key] = widget
                controls.append(widget)
                layout.addWidget(widget)
            reset = QtWidgets.QPushButton("Reset this group")
            reset.clicked.connect(lambda _=False, rows=controls: self._reset(rows))
            layout.addWidget(reset)
            layout.addStretch()
            scroll = QtWidgets.QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(page)
            self._groups.addItem(title)
            self._pages.addWidget(scroll)
        advanced = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(advanced)
        form.addRow("Voice fidelity", self._numbers["guidance"])
        form.addRow("Output headroom", self._numbers["headroom"])
        variation = QtWidgets.QHBoxLayout()
        variation.addWidget(self._seed)
        randomize = QtWidgets.QPushButton("New variation")
        randomize.clicked.connect(lambda: self._seed.setValue(secrets.randbelow(2147483648)))
        variation.addWidget(randomize)
        form.addRow("Variation number", variation)
        for key, note in {"guidance": "Higher values follow the speaker reference more closely; 1 is the neutral model default.",
                          "headroom": "Leave this much output level below clipping. Higher values are quieter."}.items():
            self._numbers[key].setToolTip(note)
        note = QtWidgets.QLabel("Style strengths are calibrated to the published model settings. Zero is neutral. "
                               "Preview your changes: combining strong styles can make speech less clear. "
                               "Reference recordings stay on this computer.")
        note.setWordWrap(True)
        form.addRow(note)
        self._groups.addItem("Advanced")
        self._pages.addWidget(advanced)

    def _visibility(self, *_args) -> None:
        """Keep Piper's simple controls distinct from Zonos expression; e.g. inactive settings never suggest unsupported Piper capabilities."""

        enabled = self._engine.currentData() == "zonos"
        self._zonos.setVisible(enabled)
        self._voice.setEnabled(not self._reference.text().strip())
        self._tabs.setVisible(enabled)
        self._quick.setVisible(enabled)

    @staticmethod
    def _reset(rows) -> None:
        """Reset a cohesive group without touching other styles; e.g. neutralize pitch while retaining a conversational delivery."""

        for row in rows:
            row.set_value(0)

    def _style(self, name: str) -> None:
        """Offer mild editable starting points; e.g. Clear combines articulation and clarity without changing the speaker."""

        styles = {"Neutral": {}, "Clear": {"mood.clarity": .35, "mood.articulation": .2},
                  "Conversational": {"delivery.conversational": .5},
                  "Lively": {"expressive.expressive": .25, "emotions.happy": .2},
                  "Calm": {"emotions.serene": .25, "mood.arousal": -.15}, "Whisper": {"delivery.whisper": .35}}
        for key, widget in self._strengths.items():
            widget.set_value(styles[name].get(key, 0))

    def _browse(self) -> None:
        """Select a local speaker reference without upload; e.g. a Dutch voice recording can be reused across all exercises."""

        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Choose voice reference", "", "Audio (*.wav *.mp3 *.flac *.m4a *.ogg);;All files (*)")
        if path:
            self._reference.setText(path)

    def load(self, cfg: listening_voices.Config) -> None:
        """Restore a complete named preset; e.g. switching the editor never discards another voice's settings."""

        self._engine.setCurrentIndex(self._engine.findData(cfg.engine))
        self._voice.setCurrentIndex(self._voice.findData(cfg.voice))
        self._reference.setText(cfg.reference)
        self._pace.setValue(cfg.pace * 100)
        self._volume.setValue(cfg.volume * 100)
        self._seed.setValue(cfg.seed)
        for key, spin in self._numbers.items():
            spin.setValue(getattr(cfg, key))
        for key, widget in self._strengths.items():
            widget.set_value(cfg.controls.get(key, 0))
        self._visibility()

    def config(self) -> listening_voices.Config:
        """Validate all visible settings together; e.g. saved and previewed voices use the same normalized controls."""

        values = {key: spin.value() for key, spin in self._numbers.items()}
        controls = {key: widget.value() for key, widget in self._strengths.items() if widget.value()}
        return listening_voices.Config(engine=self._engine.currentData(), voice=self._voice.currentData(),
                                       reference=self._reference.text().strip(), pace=self._pace.value() / 100,
                                       volume=self._volume.value() / 100, seed=self._seed.value(), controls=controls, **values)

    def neutral(self) -> None:
        """Remove expressive adjustments while keeping speaker identity; e.g. compare a stylized voice with its neutral version."""

        for widget in self._strengths.values():
            widget.set_value(0)
