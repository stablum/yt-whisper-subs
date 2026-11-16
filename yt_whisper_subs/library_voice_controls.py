"""Build native voice controls from the model's complete published direction catalogue.

Example: calibrated mood sliders and neutral reset buttons work identically for every named Zonos preset.
"""

import secrets

from PySide6 import QtCore
from PySide6 import QtWidgets

from yt_whisper_subs import i18n
from yt_whisper_subs import listening_voices


class Strength(QtWidgets.QWidget):
    """Present one expressive axis in everyday terms; e.g. lower ↔ higher pitch around a neutral zero."""

    changed = QtCore.Signal()

    def __init__(self, definition: dict) -> None:
        """Keep a percentage slider and exact entry synchronized; e.g. 100% uses the published calibrated dose."""

        super().__init__()
        self.key = definition["id"]
        self._slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self._spin = QtWidgets.QDoubleSpinBox()
        self._spin.setDecimals(2)
        self._spin.setSingleStep(1)
        minimum = -100 if definition["axis"] else 0
        for widget in (self._slider, self._spin):
            widget.setRange(minimum, 100)
        self._spin.setSuffix(" %")
        self._spin.setMinimumWidth(86)
        self._slider.setSingleStep(5)
        self._slider.setTickPosition(QtWidgets.QSlider.TickPosition.TicksBelow)
        self._slider.setTickInterval(50)
        self._slider.valueChanged.connect(lambda: self._sync(self._slider))
        self._spin.valueChanged.connect(lambda: self._sync(self._spin))
        poles = i18n.tr('{negative}  ←  neutral  →  {positive}', negative=definition['negative'], positive=definition['positive']) if minimum else i18n.tr('Neutral  →  stronger')
        title = QtWidgets.QLabel(definition["label"])
        title.setStyleSheet("font-weight:600")
        note = QtWidgets.QLabel(poles)
        note.setStyleSheet("color:#9cacc1;font-size:9pt")
        note.setWordWrap(True)
        reset = QtWidgets.QToolButton()
        reset.setText("↺")
        reset.setToolTip(i18n.tr('Reset this control to neutral'))
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

    def _sync(self, source) -> None:
        """Keep slider dragging and precise entries consistent without rounding the saved mix; e.g. a quarter of 69% remains 17.25%."""

        target = self._spin if source is self._slider else self._slider
        with QtCore.QSignalBlocker(target):
            target.setValue(source.value() if target is self._spin else round(source.value()))
        self.changed.emit()

    def set_value(self, value: float) -> None:
        """Restore a saved strength without interpreting a voice label; e.g. duplicate presets reproduce all their sliders."""

        self._spin.setValue(round(value * 100, 2))


class VoiceEditor(QtWidgets.QWidget):
    """Keep model settings editable without starting synthesis; e.g. experiment with sliders, then explicitly preview."""

    changed = QtCore.Signal()

    def __init__(self) -> None:
        """Group the complete catalogue into native pages; e.g. reference speaker identity stays separate from the Dutch exercise language."""

        super().__init__()
        self._engine = QtWidgets.QComboBox()
        self._engine.addItem(i18n.tr('Piper · Dutch Ronnie'), "piper")
        self._engine.addItem("ZONOS3-Mini", "zonos")
        self._voice = QtWidgets.QComboBox()
        self._voice.setAccessibleName(i18n.tr('Reference speaker'))
        self._voice.setToolTip(i18n.tr('The listed language belongs to the reference recording. It does not change the exercise language.'))
        for row in listening_voices.catalog()["voices"]:
            self._voice.addItem(i18n.tr(
                '{name} · {language} reference · {gender} · {style}',
                name=row['name'],
                language=row['language'],
                gender=row['gender'],
                style=row['style'],
            ), row["name"])
        self._reference = QtWidgets.QLineEdit()
        self._reference.setPlaceholderText(i18n.tr('Optional local voice recording'))
        self._reference.setClearButtonEnabled(True)
        self._reference.setToolTip(i18n.tr('Use a short, clean recording of one speaker. Leave empty to use the selected built-in voice. Audio stays local.'))
        browse = QtWidgets.QPushButton(i18n.tr('Browse…'))
        browse.clicked.connect(self._browse)
        ref_row = QtWidgets.QHBoxLayout()
        ref_row.addWidget(self._reference, 1)
        ref_row.addWidget(browse)
        self._pace = self._number(60, 150, 100, " %")
        self._volume = self._number(20, 150, 100, " %")
        self._seed = QtWidgets.QSpinBox()
        self._seed.setRange(0, 2147483647)
        self._seed.setToolTip(i18n.tr('The same variation number and settings reproduce the same take.'))
        self._numbers = {"guidance": self._number(1, 3, 1), "headroom": self._number(0, 24, 3, " dB")}
        self._expression = self._number(0, 100, 100, " %")
        self._expression.setToolTip(i18n.tr('Scale all expression sliders together. Try 25–50% if a strong mix cannot finish speaking. Pace and volume stay independent.'))
        form = QtWidgets.QFormLayout()
        form.addRow(i18n.tr('Speech engine'), self._engine)
        self._zonos = QtWidgets.QWidget()
        details = QtWidgets.QFormLayout(self._zonos)
        details.setContentsMargins(0, 0, 0, 0)
        details.addRow(i18n.tr('Reference speaker'), self._voice)
        note = QtWidgets.QLabel(i18n.tr(
            'Listening sentences are Dutch. Zonos has no built-in Dutch reference speaker; these '
            'speakers can speak Dutch, but their accent may vary. Use a Dutch recording below as a '
            'reference, or choose Piper · Dutch Ronnie.',
        ))
        note.setWordWrap(True)
        note.setStyleSheet("color:#b8c7d9;")
        details.addRow(note)
        details.addRow(i18n.tr('Clone from recording'), ref_row)
        details.addRow(i18n.tr('Overall expression'), self._expression)
        form.addRow(self._zonos)
        form.addRow(i18n.tr('Speaking pace'), self._pace)
        form.addRow(i18n.tr('Playback volume'), self._volume)
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
        quick.addWidget(QtWidgets.QLabel(i18n.tr('Starting style')))
        for name in (i18n.mark('Neutral', ctx="voice style"), i18n.mark('Clear', ctx="voice style"), i18n.mark('Conversational', ctx="voice style"), i18n.mark('Lively', ctx="voice style"), i18n.mark('Calm', ctx="voice style"), i18n.mark('Whisper', ctx="voice style")):
            button = QtWidgets.QPushButton(i18n.tr(name, ctx="voice style"))
            button.setToolTip(i18n.tr('Replace expression settings with this starting point; speaker identity stays the same.'))
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
        for widget in (self._pace, self._volume, self._seed, self._expression, *self._numbers.values()):
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
            reset = QtWidgets.QPushButton(i18n.tr('Reset this group'))
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
        form.addRow(i18n.tr('Voice fidelity'), self._numbers["guidance"])
        form.addRow(i18n.tr('Output headroom'), self._numbers["headroom"])
        variation = QtWidgets.QHBoxLayout()
        variation.addWidget(self._seed)
        randomize = QtWidgets.QPushButton(i18n.tr('New variation'))
        randomize.clicked.connect(lambda: self._seed.setValue(secrets.randbelow(2147483648)))
        variation.addWidget(randomize)
        form.addRow(i18n.tr('Variation number'), variation)
        for key, note in {"guidance": i18n.tr('Higher values follow the speaker reference more closely; 1 is the neutral model default.'),
                          "headroom": i18n.tr('Leave this much output level below clipping. Higher values are quieter.')}.items():
            self._numbers[key].setToolTip(note)
        note = QtWidgets.QLabel(i18n.tr(
            'Style strengths use the published model doses. Zero is neutral; individual ranges do not'
            ' guarantee a safe combined mix. Lower Overall expression if speech becomes unclear or '
            'cannot finish. Reference recordings stay on this computer.',
        ))
        note.setWordWrap(True)
        form.addRow(note)
        self._groups.addItem(i18n.tr('Advanced'))
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

        styles = {'Neutral': {}, 'Clear': {"mood.clarity": .35, "mood.articulation": .2},
                  'Conversational': {"delivery.conversational": .5},
                  'Lively': {"expressive.expressive": .25, "emotions.happy": .2},
                  'Calm': {"emotions.serene": .25, "mood.arousal": -.15}, 'Whisper': {"delivery.whisper": .35}}
        for key, widget in self._strengths.items():
            widget.set_value(styles[name].get(key, 0))

    def _browse(self) -> None:
        """Select a local speaker reference without upload; e.g. a Dutch voice recording can be reused across all exercises."""

        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, i18n.tr('Choose voice reference'), "", "Audio (*.wav *.mp3 *.flac *.m4a *.ogg);;All files (*)")
        if path:
            self._reference.setText(path)

    def load(self, cfg: listening_voices.Config) -> None:
        """Restore a complete named preset; e.g. switching the editor never discards another voice's settings."""

        self._engine.setCurrentIndex(self._engine.findData(cfg.engine))
        self._voice.setCurrentIndex(self._voice.findData(cfg.voice))
        self._reference.setText(cfg.reference)
        self._pace.setValue(cfg.pace * 100)
        self._volume.setValue(cfg.volume * 100)
        self._expression.setValue(cfg.expression * 100)
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
                                       volume=self._volume.value() / 100, expression=self._expression.value() / 100,
                                       seed=self._seed.value(), controls=controls, **values)

    def neutral(self) -> None:
        """Remove expressive adjustments while keeping speaker identity; e.g. compare a stylized voice with its neutral version."""

        for widget in self._strengths.values():
            widget.set_value(0)
