"""Native Dutch comprehension options and mixed-format question cards.

Example: a multiple-choice answer uses the same guarded continuation as a corrected open response.
"""

from PySide6 import QtCore
from PySide6 import QtWidgets

from yt_whisper_subs import i18n
from yt_whisper_subs import comprehension as comp
from yt_whisper_subs import library_practice_controls
from yt_whisper_subs import library_sentence_review
from yt_whisper_subs import library_written_controls as written


class Options(QtWidgets.QWidget):
    """Keep content settings together, e.g. reading and listening can have different question counts."""

    restart_requested = QtCore.Signal()
    finish_requested = QtCore.Signal()
    editing = QtCore.Signal(bool)

    def __init__(self, db, direction) -> None:
        """Restore one modality's preferences without generating text; e.g. inspecting tracking remains free of API calls."""

        super().__init__()
        self.direction = comp.Direction(direction)
        self.key = 'comprehension:' + self.direction.value + '_options'
        try:
            opts = comp.Options.model_validate_json(db.setting(self.key, '{}'))
        except ValueError:
            opts = comp.Options()
        self._genre = QtWidgets.QComboBox()
        for key, label in comp.GENRES.items():
            self._genre.addItem(i18n.tr(label), key)
        self._genre.setCurrentIndex(self._genre.findData(opts.genre))
        self._level = QtWidgets.QComboBox()
        self._level.addItems(list(comp.Level))
        self._level.setCurrentText(opts.level)
        self._numbers = {}
        panel = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(panel)
        self._current = QtWidgets.QLabel()
        self._current.setWordWrap(True)
        self._current.hide()
        form.addRow(self._current)
        heading = QtWidgets.QLabel(i18n.tr('Settings for new text'))
        font = heading.font()
        font.setBold(True)
        heading.setFont(font)
        form.addRow(heading)
        form.addRow(i18n.tr('Text type'), self._genre)
        form.addRow(i18n.tr('Language level'), self._level)
        for key, label, low, high in (('paragraphs', i18n.tr('Paragraphs'), 2, 10), ('mc', i18n.tr('Multiple-choice questions'), 0, 20),
                                      ('open', i18n.tr('Open questions'), 0, 15), ('words', i18n.tr('Maximum words from your vocabulary'), 0, 20)):
            spin = QtWidgets.QSpinBox()
            spin.setRange(low, high)
            spin.setValue(getattr(opts, key))
            self._numbers[key] = spin
            form.addRow(label, spin)
        self._early = QtWidgets.QCheckBox(i18n.tr('Include words not due yet'))
        self._early.setChecked(opts.include_early)
        form.addRow(self._early)
        note = QtWidgets.QLabel(i18n.tr('1–30 questions in total. Each multiple-choice question has one correct answer.\nOpen answers also receive partial comprehension credit.'))
        note.setWordWrap(True)
        form.addRow(note)
        start = QtWidgets.QPushButton(i18n.tr('Prepare new text'))
        start.setAutoDefault(False)
        start.clicked.connect(self._restart)
        form.addRow(start)
        self.menu = QtWidgets.QMenu(self)
        fields = QtWidgets.QWidgetAction(self.menu)
        fields.setDefaultWidget(panel)
        self.menu.addAction(fields)
        self.menu.addSeparator()
        self._finish = self.menu.addAction(i18n.tr('End this round'), self.finish_requested)
        self.menu.aboutToShow.connect(lambda: self.editing.emit(True))
        self.menu.aboutToHide.connect(lambda: self.editing.emit(False))
        button = QtWidgets.QToolButton()
        button.setObjectName('practiceOptions')
        button.setText(i18n.tr('Round options'))
        button.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        button.setMenu(self.menu)
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(button)

    def options(self) -> comp.Options:
        """Validate the complete next-round request; e.g. zero choices and eight open questions is a supported exercise."""

        values = {key: spin.value() for key, spin in self._numbers.items()}
        return comp.Options(direction=self.direction, genre=self._genre.currentData(), level=self._level.currentText(),
                            include_early=self._early.isChecked(), **values)

    def _restart(self) -> None:
        """Close the popover before exposing content; e.g. Enter in a count field cannot accidentally submit a question."""

        self.menu.close()
        self.restart_requested.emit()

    def set_round_counts(self, counts: tuple[int, int] | None) -> None:
        """Show the displayed question mix independently of editable settings, e.g. when resuming an older round."""

        if counts is not None:
            mc, open_count = counts  # Multiple-choice and open questions.
            self._current.setText(i18n.tr('This round · {mc} multiple-choice · {open} open', mc=mc, open=open_count))
        self._current.setVisible(counts is not None)

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
        self.editor.setPlaceholderText(i18n.tr('Write your answer in Dutch…'))
        self.editor.setAccessibleName(i18n.tr('Your open answer'))

    def idle(self, title, message) -> None:
        """Clear both formats while keeping a recoverable state; e.g. a failed preparation cannot leave old answer buttons visible."""

        super().idle(title, message)
        self.choices.hide()
        self.choices.shortcuts(False)

    def present(self, row, draft='') -> None:
        """Show only the public question and saved learner text; e.g. answer keys and criterion solutions remain concealed."""

        q = row['content']
        self.show_question(written.Question(source=q['prompt'], hint=i18n.tr(comp.SKILLS[q['skill']]),
                           instruction=i18n.tr('Choose one answer.') if q['kind'] == 'mc' else i18n.tr('Answer the question in Dutch. You may use your own wording.'),
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
        title = i18n.tr('Well understood') if row['score'] == 1 else i18n.tr('Partly understood') if row['score'] else i18n.tr('Review the explanation')
        language = '' if row['language_correct'] is None else i18n.tr(' · language correct') if row['language_correct'] else i18n.tr(' · language errors corrected')
        self._verdict.setText(i18n.tr('{title} · comprehension {score:.0%}{language}', title=title, score=row['score'], language=language))
        details = [grade['explanation']]
        criteria = {c['key']: c['text'] for c in q['rubric']}
        details.extend(f"{'✓' if c['met'] else '✕'} {criteria[c['key']]} — {c['explanation']}" for c in grade.get('criteria', []))
        details.extend(f"{e['evidence'] or i18n.tr('Missing')} → {e['correction']}\n{e['explanation']}" for e in grade.get('errors', []))
        details.extend(i18n.tr('Paragraph {paragraph}: ‘{quote}’', paragraph=e['paragraph'], quote=e['quote']) for e in grade['evidence'])
        self._details.setText('\n\n'.join(details))
        if q['kind'] == 'mc':
            self.choices.reveal(q['correct_idx'], row['selected_idx'])
            self.editor.hide()
        else:
            self.editor.annotate(grade, 'en_nl')

    def pending(self, row, *, withheld=False) -> None:
        """Keep a submitted first attempt immutable while grading runs; e.g. listening feedback cannot reveal the source before the last answer."""

        state = row['check_state']
        message = (i18n.tr('Answer saved. Explanations appear after all answers are submitted.') if withheld else
                   i18n.tr('Preparing correction…') if state == 'checking' else
                   i18n.tr('Correction failed. Your answer is saved; retry the correction.') if state == 'failed' else
                   i18n.tr('Answer saved · correction queued.'))
        self.message(message)
        self.choices.shortcuts(False)
        self.arm()
