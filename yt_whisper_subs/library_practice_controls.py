"""Focused native controls for configuring, answering and continuing practice.

Example: one answer click reveals feedback, then a separate click continues.
"""

from __future__ import annotations

import html
import re
import textwrap

from PySide6 import QtCore
from PySide6 import QtGui
from PySide6 import QtWidgets

from yt_whisper_subs import practice


class QuizContext(QtWidgets.QLabel):
    """Make the tested occurrence stand out safely; e.g. highlight veiliger for a saved veilig dictionary entry."""

    def __init__(self) -> None:
        """Keep rich formatting local to the example; e.g. model-provided markup stays literal text."""

        super().__init__()
        self.setObjectName("practiceContext")
        self.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def message(self, text: str) -> None:
        """Render loading and empty messages as text; e.g. an API error containing angle brackets cannot become markup."""

        self.setText(html.escape(text).replace("\n", "<br>"))

    def present(self, row: dict) -> None:
        """Highlight every full-word occurrence or reverse-answer gap; e.g. de never lights up the prefix in deze."""

        content, ctx = row["content"], row["lookup_context"]
        text = content["context"]
        if row["direction"] == practice.Direction.EN_NL:
            pattern = r"(?<!_)_{4}(?!_)"
        else:
            form = content["surface"]
            pattern = rf"(?<!\w){re.escape(form)}(?!\w)"
            if not re.search(pattern, text, re.IGNORECASE) and ctx["span"]:
                start, end = ctx["span"]
                form = ctx["sentence"][start:end]
                pattern = rf"(?<!\w){re.escape(form)}(?!\w)" if form else r"(?!)"
        parts, end = [], 0
        for match in re.finditer(pattern, text, re.IGNORECASE):
            parts.extend([html.escape(text[end:match.start()]),
                          '<span style="color:#ffd166; background-color:#3b3220; font-weight:700;">',
                          html.escape(match.group()), "</span>"])
            end = match.end()
        parts.append(html.escape(text[end:]))
        self.setText("".join(parts).replace("\n", "<br>"))


class PracticeButton(QtWidgets.QPushButton):
    """Require fresh key presses for study actions; e.g. holding Enter cannot launch the next round."""

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:
        """Ignore system key repeats; e.g. one held Space is one deliberate action."""

        if event.isAutoRepeat():
            event.accept()
        else:
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QtGui.QKeyEvent) -> None:
        """Ignore synthetic repeat releases too; e.g. Space activates only on its actual release."""

        if event.isAutoRepeat():
            event.accept()
        else:
            super().keyReleaseEvent(event)


class ContinueButton(PracticeButton):
    """Handle continuation keys explicitly; e.g. do not depend on dialog-default focus restoration after feedback."""

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:
        """Use one fresh Space/Enter press as one action; e.g. ignore system repeats while moving between cards."""

        if event.key() in (QtCore.Qt.Key.Key_Space, QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter):
            if not event.isAutoRepeat():
                self.click()
            event.accept()
        else:
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QtGui.QKeyEvent) -> None:
        """Consume the matching release without a second activation; e.g. a Space press already continued."""

        if event.key() in (QtCore.Qt.Key.Key_Space, QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter):
            event.accept()
        else:
            super().keyReleaseEvent(event)


class RoundControls(QtWidgets.QWidget):
    """Keep occasional setup outside the question path; e.g. adjust the next round in a native popover."""

    restart_requested = QtCore.Signal()
    finish_requested = QtCore.Signal()
    editing = QtCore.Signal(bool)

    def __init__(self) -> None:
        """Offer settings and explicit restart together; e.g. count entry never triggers answer shortcuts."""

        super().__init__()
        self.direction = QtWidgets.QComboBox()
        for direction in practice.Direction:
            self.direction.addItem(direction.label, direction)
        self.count = QtWidgets.QSpinBox()
        self.count.setRange(1, 50)
        self.count.setValue(10)
        self.count.setSuffix(" terms")
        self.early = QtWidgets.QCheckBox("Include words not due yet")
        apply = QtWidgets.QPushButton("New round")
        apply.setAutoDefault(False)
        apply.setObjectName("primaryButton")
        apply.clicked.connect(self._restart)
        note = QtWidgets.QLabel("Use these settings for a new round. Your answers so far stay saved.")
        note.setObjectName("settingsNote")
        note.setWordWrap(True)
        panel = QtWidgets.QWidget()
        panel.setMinimumWidth(330)
        form = QtWidgets.QFormLayout(panel)
        form.addRow("Direction", self.direction)
        form.addRow("Round length", self.count)
        form.addRow(self.early)
        form.addRow(note)
        form.addRow(apply)
        self.menu = QtWidgets.QMenu(self)
        fields = QtWidgets.QWidgetAction(self.menu)
        fields.setDefaultWidget(panel)
        self.menu.addAction(fields)
        self.menu.addSeparator()
        self._finish = self.menu.addAction("End this round", self.finish_requested)
        self._finish.setEnabled(False)
        self.menu.aboutToShow.connect(lambda: self.editing.emit(True))
        self.menu.aboutToHide.connect(lambda: self.editing.emit(False))
        button = QtWidgets.QToolButton()
        button.setObjectName("practiceOptions")
        button.setText("Round options")
        button.setMinimumHeight(32)
        button.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        button.setMenu(self.menu)
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(button)

    def _restart(self) -> None:
        """Close the editor before changing rounds; e.g. preserve focus and avoid a popup over the next question."""

        self.menu.close()
        self.restart_requested.emit()

    def set_active(self, active: bool) -> None:
        """Enable explicit ending only for a live round; e.g. no meaningless End action on a summary."""

        self._finish.setEnabled(active)

    def options(self) -> practice.RoundOptions:
        """Validate the native controls as one value; e.g. a twelve-term reverse round."""

        return practice.RoundOptions(direction=practice.Direction(self.direction.currentData()),
                                     count=self.count.value(), include_early=self.early.isChecked())

    def restore(self, document: str) -> None:
        """Restore saved defaults independently of a resumable round; e.g. retain its original direction and choices."""

        try:
            opts = practice.RoundOptions.model_validate_json(document)
        except ValueError:
            return
        self.direction.setCurrentIndex(self.direction.findData(opts.direction))
        self.count.setValue(opts.count)
        self.early.setChecked(opts.include_early)


class AnswerButtons(QtWidgets.QWidget):
    """Keep answer targets usable during feedback; e.g. click the same large button again to continue."""

    selected = QtCore.Signal(int)

    def __init__(self) -> None:
        """Tie numeric access to actual buttons; e.g. remove answer shortcuts while editing settings or reading feedback."""

        super().__init__()
        self._guard = QtCore.QElapsedTimer()
        self._buttons = [PracticeButton() for _ in range(3)]
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        for idx, button in enumerate(self._buttons):
            button.setObjectName("practiceChoice")
            button.setMinimumHeight(82)
            button.setAutoDefault(False)
            button.clicked.connect(lambda _checked=False, choice=idx: self._select(choice))
            layout.addWidget(button)

    def _select(self, idx: int) -> None:
        """Reject a leftover click after continuation; e.g. its double-click cannot answer the newly shown word."""

        if not self._guard.isValid() or self._guard.elapsed() >= QtWidgets.QApplication.doubleClickInterval():
            self.selected.emit(idx)

    def shortcuts(self, enabled: bool) -> None:
        """Separate answer keys from continuation; e.g. holding 1 cannot answer another question after feedback."""

        for idx, button in enumerate(self._buttons, 1):
            button.setShortcut(QtGui.QKeySequence(str(idx) if enabled else ""))

    def present(self, choices: list[dict], *, guard: bool = False) -> None:
        """Reset colors without changing stored ordering; e.g. reopening shows the same question."""

        self.setEnabled(True)
        self._guard.invalidate()
        if guard:
            self._guard.start()
        self.shortcuts(True)
        for idx, button in enumerate(self._buttons):
            button.setText(f"{idx + 1}    {textwrap.fill(choices[idx]['text'], 55)}")
            self._style(button, "")
        self.show()

    def reveal(self, correct: int, selected: int) -> None:
        """Highlight feedback while retaining clickable targets; e.g. the correct choice stays green."""

        self.shortcuts(False)
        for idx, button in enumerate(self._buttons):
            self._style(button, "correct" if idx == correct else ("wrong" if idx == selected else ""))

    @staticmethod
    def _style(button: QtWidgets.QPushButton, state: str) -> None:
        """Refresh dynamic Qt styling for each card; e.g. clear the previous green answer."""

        button.setProperty("answerState", state)
        button.style().unpolish(button)
        button.style().polish(button)


class ContinueSurface(QtWidgets.QWidget):
    """Turn the reading area into a guarded continue target; e.g. a double-click must not skip feedback."""

    continued = QtCore.Signal()

    def __init__(self) -> None:
        """Use the system's double-click interval as a short input guard; e.g. separate choosing from continuing."""

        super().__init__()
        self._guard = QtCore.QElapsedTimer()

    def reset(self) -> None:
        """Disarm continuation before the next question; e.g. an old click cannot select its first answer."""

        self._guard.invalidate()
        self.unsetCursor()

    def arm(self) -> None:
        """Require a fresh click after revealing feedback; e.g. preserve time to see the correct answer."""

        self._guard.start()
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)

    def request_continue(self) -> None:
        """Accept one deliberate continuation; e.g. answer buttons and the footer share this gate."""

        if self._guard.isValid() and self._guard.elapsed() >= QtWidgets.QApplication.doubleClickInterval():
            self.reset()
            self.continued.emit()

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:
        """Allow clicking the card background after feedback; e.g. avoid finding a smaller navigation button."""

        if event.button() == QtCore.Qt.MouseButton.LeftButton and self._guard.isValid():
            self.request_continue()
            event.accept()
            return
        super().mouseReleaseEvent(event)
