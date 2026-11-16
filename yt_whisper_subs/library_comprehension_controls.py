"""Native Dutch comprehension options and mixed-format question cards.

Example: a multiple-choice answer uses the same guarded continuation as a corrected open response.
"""

from PySide6 import QtCore
from PySide6 import QtWidgets

from yt_whisper_subs import comprehension as comp
from yt_whisper_subs import library_practice_controls
from yt_whisper_subs import library_sentence_review
from yt_whisper_subs import library_written_controls as written
from yt_whisper_subs import listening_voices


class Options(QtWidgets.QWidget):
    """Keep independent question counts and text settings together; e.g. listening's narrator is selected before preparation."""

    restart_requested = QtCore.Signal()
    finish_requested = QtCore.Signal()
    editing = QtCore.Signal(bool)

    def __init__(self, db, direction) -> None:
        """Restore one modality's preferences without generating text; e.g. inspecting tracking remains free of API calls."""

        super().__init__()
        self._db = db
        self.direction = comp.Direction(direction)
        self.key = 'comprehension:' + self.direction.value + '_options'
        try:
            opts = comp.Options.model_validate_json(db.setting(self.key, '{}'))
        except ValueError:
            opts = comp.Options()
        self._genre = QtWidgets.QComboBox()
        for key, label in comp.GENRES.items():
            self._genre.addItem(label, key)
        self._genre.setCurrentIndex(self._genre.findData(opts.genre))
        self._level = QtWidgets.QComboBox()
        self._level.addItems(['B1', 'B2', 'C1'])
        self._level.setCurrentText(opts.level)
        self._numbers = {}
        panel = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(panel)
        form.addRow('Tekstsoort', self._genre)
        form.addRow('Taalniveau', self._level)
        for key, label, low, high in (('paragraphs', "Alinea's", 2, 10), ('mc', 'Meerkeuzevragen', 0, 20),
                                      ('open', 'Open vragen', 0, 15), ('words', 'Woorden uit je woordenschat', 1, 20)):
            spin = QtWidgets.QSpinBox()
            spin.setRange(low, high)
            spin.setValue(getattr(opts, key))
            self._numbers[key] = spin
            form.addRow(label, spin)
        self._early = QtWidgets.QCheckBox('Ook woorden oefenen die nog niet aan herhaling toe zijn')
        self._early.setChecked(opts.include_early)
        form.addRow(self._early)
        self._voice = QtWidgets.QComboBox()
        for preset in listening_voices.read(db).enabled:
            self._voice.addItem(preset.name, preset.id)
        if self._voice.findData(opts.voice_id) >= 0:
            self._voice.setCurrentIndex(self._voice.findData(opts.voice_id))
        if self.direction == comp.Direction.LISTEN:
            form.addRow('Verteller voor de volgende ronde', self._voice)
            voices = QtWidgets.QPushButton('Stemmen instellen…')
            voices.setAutoDefault(False)
            voices.clicked.connect(self._configure_voices)
            form.addRow(voices)
        note = QtWidgets.QLabel('Samen 1–30 vragen. Eén antwoord per meerkeuzevraag is juist.\nOpen antwoorden krijgen ook deelpunten voor begrip.')
        note.setWordWrap(True)
        form.addRow(note)
        start = QtWidgets.QPushButton('Nieuwe tekst voorbereiden')
        start.setAutoDefault(False)
        start.clicked.connect(self._restart)
        form.addRow(start)
        self.menu = QtWidgets.QMenu(self)
        fields = QtWidgets.QWidgetAction(self.menu)
        fields.setDefaultWidget(panel)
        self.menu.addAction(fields)
        self.menu.addSeparator()
        self._finish = self.menu.addAction('Deze ronde beëindigen', self.finish_requested)
        self.menu.aboutToShow.connect(lambda: self.editing.emit(True))
        self.menu.aboutToHide.connect(lambda: self.editing.emit(False))
        button = QtWidgets.QToolButton()
        button.setText('Ronde-opties')
        button.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        button.setMenu(self.menu)
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(button)

    def options(self) -> comp.Options:
        """Validate the complete next-round request; e.g. zero choices and eight open questions is a supported exercise."""

        values = {key: spin.value() for key, spin in self._numbers.items()}
        return comp.Options(direction=self.direction, genre=self._genre.currentData(), level=self._level.currentText(),
                            include_early=self._early.isChecked(), voice_id=self._voice.currentData() or '', **values)

    def _restart(self) -> None:
        """Close the popover before exposing content; e.g. Enter in a count field cannot accidentally submit a question."""

        self.menu.close()
        self.restart_requested.emit()

    def _configure_voices(self) -> None:
        """Reuse the complete existing voice editor; e.g. a new Zonos preset becomes selectable without reopening practice."""

        from yt_whisper_subs import library_voice_config

        self.menu.close()
        selected = self._voice.currentData()
        if library_voice_config.VoiceDialog(self._db, self).exec() == QtWidgets.QDialog.DialogCode.Accepted:
            self._voice.clear()
            for preset in listening_voices.read(self._db).enabled:
                self._voice.addItem(preset.name, preset.id)
            self._voice.setCurrentIndex(max(0, self._voice.findData(selected)))

    def set_active(self, active: bool) -> None:
        """Offer ending only for an actual active round; e.g. a prepared spare does not count as started practice."""

        self._finish.setEnabled(active)


class Card(written.WritingCard):
    """Render Dutch choices or an open response with shared hover corrections; e.g. the private English metadata never reaches labels."""

    chosen = QtCore.Signal(int)

    def __init__(self) -> None:
        """Compose existing input and choice controls; e.g. four long choices wrap inside the passage/question split."""

        super().__init__(editor=library_sentence_review.ReviewEdit())
        self.choices = library_practice_controls.AnswerButtons(count=4, wrap=37)
        self.choices.setStyleSheet('QPushButton {font-size:14pt;text-align:left;padding:10px 14px;}')
        self.choices.selected.connect(self.chosen)
        self.layout().insertWidget(4, self.choices)
        self._source.setStyleSheet('font-size:20pt;font-weight:600;color:#ffd166;padding:8px 0;')
        self.editor.setPlaceholderText('Schrijf je antwoord in het Nederlands…')
        self.editor.setAccessibleName('Jouw open antwoord')

    def idle(self, title, message) -> None:
        """Clear both formats while keeping a recoverable state; e.g. a failed preparation cannot leave old answer buttons visible."""

        super().idle(title, message)
        self.choices.hide()
        self.choices.shortcuts(False)

    def present(self, row, draft='') -> None:
        """Show only the public question and saved learner text; e.g. answer keys and criterion solutions remain concealed."""

        q = row['content']
        self.show_question(written.Question(source=q['prompt'], hint=comp.SKILLS[q['skill']],
                           instruction='Kies één antwoord.' if q['kind'] == 'mc' else 'Beantwoord de vraag in het Nederlands. Een eigen formulering mag.',
                           draft=row['answer_text']))
        if q['kind'] == 'mc':
            self.editor.hide()
            self.choices.present(q['choices'], guard=True)
            self.choices.shortcuts(row['answer_text'] is None)
        else:
            self.choices.hide()
            self.choices.shortcuts(False)
            if row['answer_text'] is None:
                self.editor.setPlainText(draft)

    def feedback(self, row) -> None:
        """Reveal supported content, rubric points and minimal Dutch corrections; e.g. grammar problems remain separate from comprehension credit."""

        grade, q = row['grade'], row['content']
        self.show_feedback(written.Feedback(correct=row['score'] == 1, corrected=grade['corrected'], translation='',
                                            explanation=grade['explanation'], errors=[]))
        title = 'Goed begrepen' if row['score'] == 1 else 'Gedeeltelijk begrepen' if row['score'] else 'Bekijk de uitleg'
        language = '' if row['language_correct'] is None else ' · taal correct' if row['language_correct'] else ' · taalfouten gecorrigeerd'
        self._verdict.setText(f"{title} · begrip {row['score']:.0%}{language}")
        details = [grade['explanation']]
        criteria = {c['key']: c['text'] for c in q['rubric']}
        details.extend(f"{'✓' if c['met'] else '✕'} {criteria[c['key']]} — {c['explanation']}" for c in grade.get('criteria', []))
        details.extend(f"{e['evidence'] or 'Ontbreekt'} → {e['correction']}\n{e['explanation']}" for e in grade.get('errors', []))
        details.extend(f"Alinea {e['paragraph']}: ‘{e['quote']}’" for e in grade['evidence'])
        self._details.setText('\n\n'.join(details))
        if q['kind'] == 'mc':
            self.choices.reveal(q['correct_idx'], row['selected_idx'])
            self.editor.hide()
        else:
            self.editor.annotate(grade, 'en_nl')

    def pending(self, row, *, withheld=False) -> None:
        """Keep a submitted first attempt immutable while grading runs; e.g. listening feedback cannot reveal the source before the last answer."""

        state = row['check_state']
        message = ('Antwoord opgeslagen. Je ziet de uitleg nadat alle antwoorden zijn ingediend.' if withheld else
                   'De beoordeling wordt voorbereid…' if state == 'checking' else
                   'Beoordeling mislukt. Je antwoord is bewaard; probeer de beoordeling opnieuw.' if state == 'failed' else
                   'Antwoord opgeslagen · beoordeling staat in de wachtrij.')
        self.message(message)
        self.choices.shortcuts(False)
        self.arm()
