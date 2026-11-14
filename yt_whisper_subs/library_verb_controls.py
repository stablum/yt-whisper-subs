"""Verb-specific round options and grammar task presentation.

Example: source-English help is available for a transformation, while corrected English appears after submission.
"""

from __future__ import annotations

from PySide6 import QtCore
from PySide6 import QtGui
from PySide6 import QtWidgets

from yt_whisper_subs import library_written_controls as written
from yt_whisper_subs import verb_practice as verbs


class VerbOptions(QtWidgets.QWidget):
    """Filter grammar targets without cluttering the exercise; e.g. choose only subjunctives for the next round."""

    restart_requested = QtCore.Signal()
    finish_requested = QtCore.Signal()
    editing = QtCore.Signal(bool)

    def __init__(self, saved: str) -> None:
        """Restore optional filters defensively; e.g. invalid combinations produce an actionable empty state."""

        super().__init__()
        self._focus = QtWidgets.QComboBox()
        for value, label in {**verbs.FOCUSES, **verbs.TENSE_LABELS}.items():
            self._focus.addItem(label, value)
        self._subject = QtWidgets.QComboBox()
        self._structure = QtWidgets.QComboBox()
        self._source = QtWidgets.QComboBox()
        for value, label in verbs.SOURCES.items():
            self._source.addItem(label, value)
        for combo, choices in ((self._subject, verbs.SUBJECTS), (self._structure, verbs.STRUCTURES)):
            combo.addItem("All · adaptive", "all")
            for value, label in choices.items():
                combo.addItem(label, value)
        self._count = QtWidgets.QSpinBox()
        self._count.setRange(1, 50)
        self._early = QtWidgets.QCheckBox("Include forms not due yet")
        try:
            opts = verbs.Options.model_validate_json(saved) if saved else verbs.Options()
        except ValueError:
            opts = verbs.Options()
        self._count.setValue(opts.count)
        self._early.setChecked(opts.include_early)
        for combo, value in ((self._focus, opts.focus), (self._subject, opts.subject),
                             (self._structure, opts.structure), (self._source, opts.source)):
            combo.setCurrentIndex(combo.findData(value))
        panel = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(panel)
        for label, widget in (("Verbs", self._source), ("Focus", self._focus), ("Subject", self._subject), ("Situation", self._structure),
                              ("Exercises", self._count)):
            form.addRow(label, widget)
        form.addRow(self._early)
        note = QtWidgets.QLabel("Subjunctives use established formal expressions.\nOnly grammatically valid combinations are offered.")
        note.setObjectName("settingsNote")
        form.addRow(note)
        restart = QtWidgets.QPushButton("New verb round")
        restart.setAutoDefault(False)
        restart.setObjectName("primaryButton")
        restart.clicked.connect(self._restart)
        form.addRow(restart)
        self.menu = QtWidgets.QMenu(self)
        action = QtWidgets.QWidgetAction(self.menu)
        action.setDefaultWidget(panel)
        self.menu.addAction(action)
        self.menu.addSeparator()
        self._finish = self.menu.addAction("End this verb round", self.finish_requested)
        self.menu.aboutToShow.connect(lambda: self.editing.emit(True))
        self.menu.aboutToHide.connect(lambda: self.editing.emit(False))
        button = QtWidgets.QToolButton()
        button.setObjectName("practiceOptions")
        button.setText("Verb round options")
        button.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        button.setMenu(self.menu)
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(button)

    def _restart(self) -> None:
        """Close editing before preparing; e.g. the popup cannot cover a new question."""

        self.menu.close()
        self.restart_requested.emit()

    def options(self) -> verbs.Options:
        """Read the domain contract from controls; e.g. early review is an explicit preference."""

        return verbs.Options(count=self._count.value(), focus=self._focus.currentData(), subject=self._subject.currentData(),
                             structure=self._structure.currentData(), source=self._source.currentData(),
                             include_early=self._early.isChecked())

    def set_active(self, active: bool) -> None:
        """Offer ending only for a live round; e.g. statistics alone does not create a round."""

        self._finish.setEnabled(active)



class VerbCard(written.WritingCard):
    """Format verb targets without coupling the shared renderer to a conjugation syllabus."""

    def present(self, row: dict) -> None:
        """Expose the source and locally owned transformation requirements; e.g. the target answer stays hidden."""

        self.show_question(written.Question(source=row["content"]["source"],
                                            hint=f"{row['lemma']}  ·  {verbs.target_label(row)}",
                                            instruction=row["content"]["instruction"], draft=row["answer_text"],
                                            source_meaning=row["content"].get("source_english"), allow_meaning=True))

    def feedback(self, row: dict) -> None:
        """Display the corrected form and its English meaning; e.g. prior typing remains visible above the correction."""

        grade = row["grade"]
        self.show_feedback(written.Feedback(correct=grade["correct"], corrected=grade["corrected"],
                                            translation=grade["corrected_english"], explanation=grade["explanation"],
                                            errors=grade["errors"]))


class VerbView(written.WritingView):
    """Compose grammar options with the common typing surface and a scoped source-help shortcut."""

    def __init__(self, saved: str) -> None:
        """Keep Alt+E within visible verb practice; e.g. typing in other modes cannot reveal a hidden source meaning."""

        super().__init__(VerbOptions(saved), VerbCard())
        self._english_key = QtGui.QShortcut(QtGui.QKeySequence("Alt+E"), self)
        self._english_key.setContext(QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._english_key.setAutoRepeat(False)
        self._english_key.activated.connect(self.card.meaning.toggle)
