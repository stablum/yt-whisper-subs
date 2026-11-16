"""Shared native sentence input, optional source help and correction rendering.

Example: whole-sentence translation hides the opposite-language answer while verb tasks may reveal their original meaning.
"""

from __future__ import annotations

import pydantic as pd
from PySide6 import QtCore
from PySide6 import QtGui
from PySide6 import QtWidgets

from yt_whisper_subs import i18n
from yt_whisper_subs import library_practice_labels
from yt_whisper_subs import library_practice_controls as controls


class Question(pd.BaseModel):
    """Supply only display-safe task fields; e.g. the expected translated answer is deliberately absent."""

    source: str
    hint: str
    instruction: str
    draft: str | None = None
    source_meaning: str | None = None
    allow_meaning: bool = False


class Feedback(pd.BaseModel):
    """Normalize domain feedback for one renderer; e.g. sentence vocabulary errors and verb errors share concrete spans."""

    correct: bool
    corrected: str
    translation: str
    explanation: str
    errors: list[dict]
    targets: str = ""
    vocabulary: list[dict] = pd.Field(default_factory=list)
    summary: str = ""


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


class SentenceMeaning(QtWidgets.QWidget):
    """Reveal cached source English in one action; e.g. the transformation answer stays hidden until submission."""

    requested = QtCore.Signal()

    def __init__(self) -> None:
        """Keep a quiet inline disclosure beneath the source; e.g. keyboard and mouse share the same toggle."""

        super().__init__()
        self._text: str | None = None
        self._button = QtWidgets.QToolButton()
        self._button.setText(i18n.tr('Show English · Alt+E'))
        self._button.setCheckable(True)
        self._button.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self._button.setAutoRepeat(False)
        self._button.toggled.connect(self._toggle)
        self._label = QtWidgets.QLabel()
        self._label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self._label.setWordWrap(True)
        self._label.setStyleSheet("font-size: 15pt; color: #91d9ef;")
        self._label.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._button, 0, QtCore.Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self._label)
        self.reset(None)

    def reset(self, text: str | None) -> None:
        """Hide meanings on every new task; e.g. the previous sentence's translation cannot leak into the next."""

        self._text = text
        self._button.setEnabled(True)
        self._button.setChecked(False)
        self._button.setText(i18n.tr('Show English · Alt+E'))
        self._label.clear()
        self._label.hide()

    def toggle(self) -> None:
        """Support a scoped keyboard shortcut without consuming typed letters; e.g. ordinary e stays in the answer."""

        if self.isVisible() and self._button.isEnabled() and QtWidgets.QApplication.activePopupWidget() is None:
            self._button.toggle()

    def _toggle(self, shown: bool) -> None:
        """Reveal immediately when available; e.g. newly prepared exercises need no extra API call."""

        self._button.setText(i18n.tr('Hide English · Alt+E') if shown else i18n.tr('Show English · Alt+E'))
        self._label.setVisible(shown)
        if shown:
            self._label.setText(self._text or i18n.tr('Translating…'))
            if not self._text:
                self._button.setEnabled(False)
                self.requested.emit()

    def translated(self, text: str) -> None:
        """Complete help without changing answer focus; e.g. a source translation can arrive during typing."""

        self._text = text
        self._button.setEnabled(True)
        self._label.setText(text)

    def failed(self, message: str) -> None:
        """Offer a retry without losing typed input; e.g. failed sentence help cannot count as a wrong answer."""

        self._button.setChecked(False)
        self._button.setEnabled(True)
        self._button.setText(i18n.tr('Retry English · Alt+E'))
        self._label.setText(i18n.tr('Could not translate: {message}', message=message))
        self._label.show()



class WritingCard(controls.ContinueSurface):
    """Render typed questions and corrections; e.g. shared sentence input never owns vocabulary scheduling."""

    submitted = QtCore.Signal()

    def __init__(self, editor: SentenceEdit | None = None, source: QtWidgets.QLabel | None = None) -> None:
        """Keep the answer area spacious and stable; e.g. show the original sentence above the rewrite."""

        super().__init__()
        self._source = source if source is not None else QtWidgets.QLabel()
        self._source.setStyleSheet("font-size: 23pt; font-weight: 600; color: #ffd166; padding: 12px 0;")
        self.meaning = SentenceMeaning()
        self._target = QtWidgets.QLabel()
        self._target.setStyleSheet("font-size: 13pt; color: #c4a1ff;")
        self._instruction = QtWidgets.QLabel()
        self._instruction.setStyleSheet("font-size: 14pt; color: #dce6f2;")
        self.editor = editor if editor is not None else SentenceEdit()
        self.editor.setPlaceholderText(i18n.tr('Type the transformed Dutch sentence…'))
        self.editor.setAccessibleName(i18n.tr('Your transformed Dutch sentence'))
        self.editor.setStyleSheet("font-size: 18pt; padding: 12px; border-radius: 8px; background: #1c2431; "
                                 "color: #eef2f7; border: 1px solid #42536c; selection-background-color: #356198;")
        self.editor.setMinimumHeight(120)
        self.editor.setMaximumHeight(180)
        self.editor.submitted.connect(self.submitted)
        self._verdict = QtWidgets.QLabel()
        self._verdict.setStyleSheet("font-size: 14pt; font-weight: 600;")
        self._correction = QtWidgets.QLabel()
        self._correction.setStyleSheet("font-size: 20pt; font-weight: 600; color: #83e5af;")
        self._translation = QtWidgets.QLabel()
        self._translation.setStyleSheet("font-size: 15pt; color: #91d9ef;")
        self._targets = QtWidgets.QLabel()
        self._targets.setStyleSheet("font-size: 12pt; color: #c4a1ff;")
        self._details = QtWidgets.QLabel()
        self._details.setStyleSheet("font-size: 12pt; color: #c4d5e9;")
        body = QtWidgets.QVBoxLayout(self)
        body.setContentsMargins(12, 12, 12, 12)
        body.setSpacing(16)
        for label in (self._source, self._target, self._instruction, self._verdict, self._correction, self._translation, self._targets, self._details):
            label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            label.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        for widget in (self._source, self.meaning, self._target, self._instruction, self.editor,
                       self._verdict, self._correction, self._translation, self._targets, self._details):
            body.addWidget(widget)
        body.addStretch()

    def idle(self, title: str, message: str) -> None:
        """Display loading, empty or completion states; e.g. a retry message never reveals an answer."""

        self.reset()
        self._source.setText(title)
        self.meaning.hide()
        self.meaning.reset(None)
        self._target.clear()
        self._instruction.setText(message)
        self.editor.hide()
        self._verdict.clear()
        self._correction.clear()
        self._translation.clear()
        self._details.clear()

        self._targets.clear()
        self._targets.hide()

    def show_question(self, content: Question) -> None:
        """Render source-only task data and a saved draft; e.g. full translation answers remain hidden until submission."""

        self.reset()
        self._source.setText(content.source)
        self.meaning.reset(content.source_meaning)
        self.meaning.setVisible(content.allow_meaning)
        self._target.setText(content.hint)
        self._instruction.setText(content.instruction)
        self.editor.setPlainText(content.draft or "")
        self.editor.setReadOnly(content.draft is not None)
        self.editor.show()
        for label in (self._verdict, self._correction, self._translation, self._targets, self._details):
            label.clear()
            label.hide()
        self._targets.hide()
        if content.draft is None:
            self.editor.setFocus()

    def show_feedback(self, content: Feedback) -> None:
        """Reveal a corrected full sentence and concrete teaching feedback; e.g. mouse continuation remains a separate gesture."""

        self.editor.setReadOnly(True)
        verdict = i18n.tr('Correct') if content.correct else i18n.tr('Not quite · here is the correction')
        self._verdict.setText(verdict + (f" · {content.summary}" if content.summary else ""))
        self._correction.setText(content.corrected)
        self._translation.setText(content.translation)
        concepts = {lexeme["lemma"]: lexeme["meaning"] for fix in content.vocabulary
                    for role in ("intended", "confused") if (lexeme := fix[role]) is not None}
        labels = [content.targets] if content.targets else []
        if concepts:
            labels.append(i18n.tr('Vocabulary review: ') + ";  ".join(f"{word} — {meaning}" for word, meaning in concepts.items()))
        self._targets.setText("\n".join(labels))
        self._targets.setVisible(bool(labels))
        details = [content.explanation]
        for error in content.errors:
            evidence = error["evidence"] or i18n.tr('missing text')
            kind = library_practice_labels.term(error['kind'])
            details.append(f"• {kind}: {evidence} → {error['correction']}\n  {error['explanation']}")
        self._details.setText("\n\n".join(details))
        for label in (self._verdict, self._correction, self._translation, self._details):
            label.setVisible(bool(label.text()))
        self.arm()

    def message(self, text: str) -> None:
        """Show an inline operation message; e.g. correction failure leaves the saved answer visible."""

        self._verdict.setText(text)
        self._verdict.show()


class WritingView(QtWidgets.QWidget):
    """Compose practice controls separately from lifecycle state; e.g. tests can inspect the native sentence card."""

    def __init__(self, options: QtWidgets.QWidget, card: WritingCard) -> None:
        """Keep one primary action and a quiet footer; e.g. settings never interrupt typed sentence entry."""

        super().__init__()
        self.options = options
        self.status = QtWidgets.QLabel()
        self.status.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        header = QtWidgets.QHBoxLayout()
        header.addWidget(self.status, 1)
        header.addWidget(self.options)
        self.bar = QtWidgets.QProgressBar()
        self.bar.setFixedHeight(6)
        self.bar.setTextVisible(False)
        self.card = card
        questions = QtWidgets.QWidget()
        self.question_layout = QtWidgets.QHBoxLayout(questions)
        self.question_layout.setContentsMargins(0, 0, 0, 0)
        self.question_layout.setSpacing(12)
        self.question_layout.addWidget(self.card, 1)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(questions)
        self.action = controls.ContinueButton(i18n.tr('Check sentence'))
        self.action.setObjectName("primaryButton")
        self.action.setMinimumSize(220, 44)
        self.action.setAutoDefault(False)
        self.flag = QtWidgets.QPushButton(i18n.tr('Report exercise / correction'))
        self.flag.setAutoDefault(False)
        self.cancel = QtWidgets.QPushButton(i18n.tr('Cancel'))
        self.cancel.setAutoDefault(False)
        footer = QtWidgets.QHBoxLayout()
        footer.addWidget(self.flag)
        footer.addWidget(self.cancel)
        footer.addStretch()
        footer.addWidget(self.action)
        body = QtWidgets.QVBoxLayout(self)
        body.addLayout(header)
        body.addWidget(self.bar)
        # Keep submit/next available even when long feedback or audio controls need scrolling.
        body.addWidget(scroll, 1)
        body.addLayout(footer)
