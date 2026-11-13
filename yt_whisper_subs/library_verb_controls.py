"""Typed sentence card and occasional verb-round settings.

Example: Enter submits one sentence; a fresh Continue gesture follows the visible correction.
"""

from __future__ import annotations

from PySide6 import QtCore
from PySide6 import QtGui
from PySide6 import QtWidgets

from yt_whisper_subs import library_practice_controls as controls
from yt_whisper_subs import verb_practice as verbs


class SentenceEdit(QtWidgets.QPlainTextEdit):
    """Reserve fresh Enter for submission; e.g. held keys cannot submit a newly displayed exercise."""

    submitted = QtCore.Signal()

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:
        """Keep normal typing and Shift+Enter intact; e.g. Space remains part of the sentence."""

        if event.key() in (QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter) and not event.modifiers():
            if not event.isAutoRepeat() and not self.isReadOnly():
                self.submitted.emit()
            event.accept()
        else:
            super().keyPressEvent(event)


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
        for combo, value in ((self._focus, opts.focus), (self._subject, opts.subject), (self._structure, opts.structure)):
            combo.setCurrentIndex(combo.findData(value))
        panel = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(panel)
        for label, widget in (("Focus", self._focus), ("Subject", self._subject), ("Situation", self._structure),
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
                             structure=self._structure.currentData(), include_early=self._early.isChecked())

    def set_active(self, active: bool) -> None:
        """Offer ending only for a live round; e.g. statistics alone does not create a round."""

        self._finish.setEnabled(active)


class SentenceCard(controls.ContinueSurface):
    """Own sentence presentation and diagnostic feedback; e.g. the controller never formats correction spans."""

    submitted = QtCore.Signal()

    def __init__(self) -> None:
        """Keep the answer area spacious and stable; e.g. show the original sentence above the rewrite."""

        super().__init__()
        self._source = QtWidgets.QLabel()
        self._source.setStyleSheet("font-size: 23pt; font-weight: 600; color: #ffd166; padding: 12px 0;")
        self._target = QtWidgets.QLabel()
        self._target.setStyleSheet("font-size: 13pt; color: #c4a1ff;")
        self._instruction = QtWidgets.QLabel()
        self._instruction.setStyleSheet("font-size: 14pt; color: #dce6f2;")
        self.editor = SentenceEdit()
        self.editor.setPlaceholderText("Type the transformed Dutch sentence…")
        self.editor.setAccessibleName("Your transformed Dutch sentence")
        self.editor.setStyleSheet("font-size: 18pt; padding: 12px; border-radius: 8px; background: #1c2431; "
                                 "color: #eef2f7; border: 1px solid #42536c; selection-background-color: #356198;")
        self.editor.setMinimumHeight(120)
        self.editor.setMaximumHeight(180)
        self.editor.submitted.connect(self.submitted)
        self._verdict = QtWidgets.QLabel()
        self._verdict.setStyleSheet("font-size: 14pt; font-weight: 600;")
        self._correction = QtWidgets.QLabel()
        self._correction.setStyleSheet("font-size: 20pt; font-weight: 600; color: #83e5af;")
        self._details = QtWidgets.QLabel()
        self._details.setStyleSheet("font-size: 12pt; color: #c4d5e9;")
        body = QtWidgets.QVBoxLayout(self)
        body.setContentsMargins(12, 12, 12, 12)
        body.setSpacing(16)
        for label in (self._source, self._target, self._instruction, self._verdict, self._correction, self._details):
            label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            label.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        for widget in (self._source, self._target, self._instruction, self.editor, self._verdict, self._correction, self._details):
            body.addWidget(widget)
        body.addStretch()

    def idle(self, title: str, message: str) -> None:
        """Display loading, empty or completion states; e.g. a retry message never reveals an answer."""

        self.reset()
        self._source.setText(title)
        self._target.clear()
        self._instruction.setText(message)
        self.editor.hide()
        self._verdict.clear()
        self._correction.clear()
        self._details.clear()

    def present(self, row: dict) -> None:
        """Display only the task and any saved first attempt; e.g. pending grading survives closing the app."""

        self.reset()
        self._source.setText(row["content"]["source"])
        self._target.setText(f"{row['lemma']}  ·  {verbs.target_label(row)}")
        self._instruction.setText(row["content"]["instruction"])
        self.editor.setPlainText(row["answer_text"] or "")
        self.editor.setReadOnly(row["answer_text"] is not None)
        self.editor.show()
        self._verdict.clear()
        self._correction.clear()
        self._details.clear()
        if row["answer_text"] is None:
            self.editor.setFocus()

    def feedback(self, row: dict) -> None:
        """Highlight the correction and teach specific mistakes; e.g. separate agreement from participle errors."""

        self.editor.setReadOnly(True)
        grade = row["grade"]
        self._verdict.setText("Correct" if grade["correct"] else "Not quite · here is the correction")
        self._correction.setText(grade["corrected"])
        details = [grade["explanation"]]
        for error in grade["errors"]:
            evidence = error["evidence"] or "missing text"
            details.append(f"• {error['kind'].replace('_', ' ')}: {evidence} → {error['correction']}\n  {error['explanation']}")
        self._details.setText("\n\n".join(details))
        self.arm()

    def message(self, text: str) -> None:
        """Show an inline operation message; e.g. correction failure leaves the saved answer visible."""

        self._verdict.setText(text)


class VerbView(QtWidgets.QWidget):
    """Compose practice controls separately from lifecycle state; e.g. tests can inspect the native sentence card."""

    def __init__(self, saved: str) -> None:
        """Keep one primary action and a quiet footer; e.g. settings never interrupt typed sentence entry."""

        super().__init__()
        self.options = VerbOptions(saved)
        self.status = QtWidgets.QLabel()
        self.status.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        header = QtWidgets.QHBoxLayout()
        header.addWidget(self.status, 1)
        header.addWidget(self.options)
        self.bar = QtWidgets.QProgressBar()
        self.bar.setFixedHeight(6)
        self.bar.setTextVisible(False)
        self.card = SentenceCard()
        self.action = controls.ContinueButton("Check sentence")
        self.action.setObjectName("primaryButton")
        self.action.setMinimumSize(220, 44)
        self.action.setAutoDefault(False)
        self.flag = QtWidgets.QPushButton("Report exercise / correction")
        self.flag.setAutoDefault(False)
        self.cancel = QtWidgets.QPushButton("Cancel")
        self.cancel.setAutoDefault(False)
        footer = QtWidgets.QHBoxLayout()
        footer.addWidget(self.flag)
        footer.addWidget(self.cancel)
        footer.addStretch()
        footer.addWidget(self.action)
        body = QtWidgets.QVBoxLayout(self)
        body.addLayout(header)
        body.addWidget(self.bar)
        body.addWidget(self.card, 1)
        body.addLayout(footer)
