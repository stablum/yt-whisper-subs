"""Native whole-sentence translation in both directions, around captured vocabulary.

Example: translate a shopping sentence containing a saved adjective in a new superlative form.
"""

from __future__ import annotations

from PySide6 import QtCore
from PySide6 import QtWidgets

from yt_whisper_subs import library_written
from yt_whisper_subs import library_written_controls as written
from yt_whisper_subs import library_sentence_review
from yt_whisper_subs import library_sentence_session
from yt_whisper_subs import practice
from yt_whisper_subs import sentence_practice as sentences
from yt_whisper_subs import sentence_service
from yt_whisper_subs import sentence_score


class SentenceOptions(QtWidgets.QWidget):
    """Keep direction, context and target count in a native popover; e.g. settings entry never submits a draft."""

    restart_requested = QtCore.Signal()
    finish_requested = QtCore.Signal()
    editing = QtCore.Signal(bool)

    def __init__(self, saved: str) -> None:
        """Restore next-round preferences; e.g. English-to-Dutch writing is the default direction."""

        super().__init__()
        try:
            opts = sentences.Options.model_validate_json(saved) if saved else sentences.Options()
        except ValueError:
            opts = sentences.Options()
        self._direction = QtWidgets.QComboBox()
        for direction in (practice.Direction.EN_NL, practice.Direction.NL_EN):
            self._direction.addItem(direction.label, direction.value)
        self._direction.setCurrentIndex(self._direction.findData(opts.direction.value))
        self._scene = QtWidgets.QComboBox()
        for key, label in {"mixed": "Varied everyday situations", **sentences.SCENES}.items():
            self._scene.addItem(label, key)
        self._scene.setCurrentIndex(self._scene.findData(opts.scene))
        self._count, self._words = QtWidgets.QSpinBox(), QtWidgets.QSpinBox()
        self._count.setRange(1, 50)
        self._count.setValue(opts.count)
        self._words.setRange(1, 3)
        self._words.setValue(opts.words)
        self._early = QtWidgets.QCheckBox("Include words not due yet")
        self._early.setChecked(opts.include_early)
        panel = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(panel)
        for label, widget in (("Direction", self._direction), ("Life context", self._scene),
                              ("Sentences", self._count), ("Saved words per sentence", self._words)):
            form.addRow(label, widget)
        form.addRow(self._early)
        note = QtWidgets.QLabel("Saved words appear in new forms and situations.\nOther words are included naturally.")
        note.setObjectName("settingsNote")
        form.addRow(note)
        restart = QtWidgets.QPushButton("New sentence round")
        restart.setAutoDefault(False)
        restart.setObjectName("primaryButton")
        restart.clicked.connect(self._restart)
        form.addRow(restart)
        self.menu = QtWidgets.QMenu(self)
        fields = QtWidgets.QWidgetAction(self.menu)
        fields.setDefaultWidget(panel)
        self.menu.addAction(fields)
        self.menu.addSeparator()
        self._finish = self.menu.addAction("End this sentence round", self.finish_requested)
        self.menu.aboutToShow.connect(lambda: self.editing.emit(True))
        self.menu.aboutToHide.connect(lambda: self.editing.emit(False))
        button = QtWidgets.QToolButton()
        button.setObjectName("practiceOptions")
        button.setText("Sentence round options")
        button.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        button.setMenu(self.menu)
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(button)

    def _restart(self) -> None:
        """Close setup before preparing; e.g. the question is never displayed beneath a stale menu."""

        self.menu.close()
        self.restart_requested.emit()

    def options(self) -> sentences.Options:
        """Return a validated learning request; e.g. the companion-word count stays separate from the sentence count."""

        return sentences.Options(direction=self._direction.currentData(), scene=self._scene.currentData(),
                                  count=self._count.value(), words=self._words.value(), include_early=self._early.isChecked())

    def set_active(self, active: bool) -> None:
        """Offer End round only for a live session; e.g. statistics access cannot invent practice history."""

        self._finish.setEnabled(active)


class SentenceCard(written.WritingCard):
    """Keep translated answers hidden until grading; e.g. practised forms appear alongside the correction afterward."""

    def __init__(self) -> None:
        """Use an evidence-aware answer editor; e.g. graded words reveal their local correction on hover."""

        super().__init__(editor=library_sentence_review.ReviewEdit(), source=library_sentence_review.SourceLabel())
        self._source.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self._source.continued.connect(self.request_continue)

    def present(self, row: dict) -> None:
        """Hide both the model answer and saved-word clues; e.g. Dutch-to-English practice cannot reveal English help first."""

        direction = practice.Direction(row["direction"])
        target = "Dutch" if direction == practice.Direction.EN_NL else "English"
        source_key = "english" if direction == practice.Direction.EN_NL else "dutch"
        self.editor.setPlaceholderText(f"Type the complete {target} translation…")
        self.editor.setAccessibleName(f"Your complete {target} translation")
        self.show_question(written.Question(source=row["content"][source_key],
                                            hint=f"{direction.label}  ·  {sentences.SCENES[row['scene']]}",
                                            instruction=f"Translate the whole sentence into {target}.", draft=row["answer_text"]))

    def feedback(self, row: dict) -> None:
        """Explain corrections and identify practised concepts; e.g. a superlative's meaning and form stay visible after checking."""

        grade = row["grade"]
        score = sentence_score.partial(row["answer_text"], grade["corrected"])
        targets = [f"{' … '.join(word['dutch_forms'])} — {' … '.join(word['english_forms'])}" for word in row["content"]["words"]]
        self.show_feedback(written.Feedback(correct=grade["correct"], corrected=grade["corrected"],
                                            translation=grade["corrected_meaning"], explanation=grade["explanation"],
                                            errors=grade["errors"], targets="Practised: " + ";  ".join(targets),
                                            vocabulary=grade.get("vocabulary", []), summary=f"Partial score: {score:.0%}"))
        self.editor.annotate(grade, row["direction"])
        self._source.annotate(row)


class SentencePanel(library_sentence_session.SentenceSession):
    """Bind sentences to the common typed-practice flow; e.g. reopening retries the same pending translation."""

    def __init__(self, db, *, engine: sentence_service.SentenceService | None = None) -> None:
        """Create the view without generation; e.g. Progress and Activity can be opened without hidden sentence exposure."""

        view = written.WritingView(SentenceOptions(db.setting("sentence_options", "")), SentenceCard())
        mode = library_written.Mode(key="sentence", label="Sentence translations", tab_name="Sentences",
                                    intro="Use your saved vocabulary to understand and write natural sentences in everyday situations.",
                                    view=view, ops=library_written.SessionOps.for_db(db, "sentence"))
        super().__init__(db, mode, engine=engine or sentence_service.SentenceService(db))
