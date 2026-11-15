"""Highlight authoritative correction evidence and provide local hover explanations.

Example: hovering zekerder shows veiliger and its explanation without another language-model call.
"""

import html
import re

import pydantic as pd
from PySide6 import QtCore
from PySide6 import QtGui
from PySide6 import QtWidgets

from yt_whisper_subs import library_written_controls as written


class Mark(pd.BaseModel):
    """Keep Unicode text positions with literal teaching feedback; e.g. repeated wrong words each receive their own hover region."""

    start: int
    end: int
    correction: str
    explanation: str


def marks(text: str, grade: dict, direction: str) -> list[Mark]:
    """Locate saved evidence without guessing errors from a paraphrase; e.g. a valid synonym is never marked merely because it differs from the reference."""

    evidence = [(row["evidence"], row["correction"], row["explanation"]) for row in grade["errors"]]
    target = "dutch" if direction == "en_nl" else "english"
    evidence.extend((row["typed"], " … ".join(row[target]), row["note"]) for row in grade.get("vocabulary", []))
    return _locate(text, evidence)


def source_marks(row: dict) -> list[Mark]:
    """Map mistakes back to their source concepts; e.g. a wrong slimmere highlights cleverest in the English question."""

    source, target = ("english", "dutch") if row["direction"] == "en_nl" else ("dutch", "english")
    grade, content = row["grade"], row["content"]
    words = {word["word_id"]: word for word in content["words"]}
    evidence = []
    for error in grade["errors"]:
        forms = error.get("source_forms", [])
        if not forms:
            forms = [form for word_id in error["word_ids"] if word_id in words for form in words[word_id][source + "_forms"]]
        evidence.extend((form, error["correction"], error["explanation"]) for form in forms)
    for fix in grade.get("vocabulary", []):
        evidence.extend((form, " … ".join(fix[target]), fix["note"]) for form in fix[source])
    return _locate(content[source], evidence)


def _locate(text: str, evidence: list[tuple[str, str, str]]) -> list[Mark]:
    """Share exact whole-word evidence matching across both texts; e.g. zeker never marks the unrelated zekerdere."""

    result, seen = [], set()
    for original, correction, explanation in evidence:
        if not original:
            continue
        pattern = re.escape(original)
        pattern = (r"(?<!\w)" if original[0].isalnum() else "") + pattern
        pattern += r"(?!\w)" if original[-1].isalnum() else ""
        for found in re.finditer(pattern, text, re.IGNORECASE):
            key = found.start(), found.end(), correction, explanation
            if key not in seen:
                seen.add(key)
                result.append(Mark(start=found.start(), end=found.end(), correction=correction, explanation=explanation))
    return result


def show_tip(matching: list[Mark], global_pos, widget) -> None:
    """Render literal teaching feedback below either text; e.g. source and answer highlights share one escaped tooltip style."""

    if not matching:
        QtWidgets.QToolTip.hideText()
        return
    rows = [f"<b style='color:#83e5af'>{html.escape(row.correction)}</b><br>{html.escape(row.explanation)}" for row in matching]
    text = "<div style='max-width:440px;font-size:12pt'>" + "<br><br>".join(rows) + "</div>"
    QtWidgets.QToolTip.showText(global_pos + QtCore.QPoint(12, 20), text, widget)


class SourceLabel(QtWidgets.QLabel):
    """Keep the source sentence literal until feedback, then highlight its affected concepts; e.g. no translation is exposed during writing."""

    continued = QtCore.Signal()

    def __init__(self) -> None:
        """Use local hover links without opening URLs; e.g. clicking a highlighted phrase still continues the review."""

        super().__init__()
        self._plain = ""
        self._marks: list[Mark] = []
        self.setOpenExternalLinks(False)
        self.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.LinksAccessibleByMouse)
        self.linkHovered.connect(self._hover)
        self.linkActivated.connect(lambda _link: self.continued.emit())

    def setText(self, text: str) -> None:
        """Reset annotations whenever another question is displayed; e.g. literal angle brackets never become markup."""

        self._plain = text
        self._marks = []
        self.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        QtWidgets.QToolTip.hideText()
        super().setText(text)

    def annotate(self, row: dict) -> None:
        """Render nonoverlapping rich-text regions with all relevant explanations; e.g. grammar and lexical feedback can refer to the same source word."""

        self._marks = source_marks(row)
        points = sorted({0, len(self._plain), *(mark.start for mark in self._marks), *(mark.end for mark in self._marks)})
        parts = []
        for start, end in zip(points, points[1:]):
            text = html.escape(self._plain[start:end]).replace("\n", "<br>")
            matching = [str(idx) for idx, mark in enumerate(self._marks) if mark.start <= start and mark.end >= end]
            if matching:
                key = ",".join(matching)
                text = (f'<a href="error:{key}" style="color:#ffd166;background-color:#59333b;'
                        f'text-decoration:underline;font-weight:700">{text}</a>')
            parts.append(text)
        self.setTextFormat(QtCore.Qt.TextFormat.RichText)
        super().setText("".join(parts))

    def _hover(self, link: str) -> None:
        """Use the same correction popup as the answer editor; e.g. hovering cleverest teaches the expected slimste."""

        indices = link.removeprefix("error:").split(",") if link else []
        matching = [self._marks[int(idx)] for idx in indices if idx.isdigit() and int(idx) < len(self._marks)]
        show_tip(matching, QtGui.QCursor.pos(), self)


class ReviewEdit(written.SentenceEdit):
    """Render mistake spans while preserving selectable original input; e.g. highlighting never replaces the learner's stored answer."""

    def __init__(self) -> None:
        """Enable immediate pointer feedback; e.g. corrections appear just below the hovered word."""

        super().__init__()
        self._marks: list[Mark] = []
        self.viewport().setMouseTracking(True)
        self.textChanged.connect(self.clear_marks)

    def clear_marks(self) -> None:
        """Remove feedback before another question is typed; e.g. an old error cannot appear on a new draft."""

        self._marks.clear()
        self.setExtraSelections([])
        QtWidgets.QToolTip.hideText()

    def annotate(self, grade: dict, direction: str) -> None:
        """Style only audited evidence; e.g. red underlines and a warm background distinguish mistakes without relying on colour alone."""

        self.clear_marks()
        text = self.toPlainText()
        self._marks = marks(text, grade, direction)
        selections = []
        for mark in self._marks:
            selection = QtWidgets.QTextEdit.ExtraSelection()
            cursor = QtGui.QTextCursor(self.document())
            # Qt uses UTF-16 offsets; Python spans count Unicode code points, including emoji before the error.
            start = len(text[:mark.start].encode("utf-16-le")) // 2
            end = len(text[:mark.end].encode("utf-16-le")) // 2
            cursor.setPosition(start)
            cursor.setPosition(end, QtGui.QTextCursor.MoveMode.KeepAnchor)
            selection.cursor = cursor
            selection.format.setForeground(QtGui.QColor("#ffd166"))
            selection.format.setBackground(QtGui.QColor("#59333b"))
            selection.format.setFontWeight(QtGui.QFont.Weight.Bold)
            selection.format.setUnderlineStyle(QtGui.QTextCharFormat.UnderlineStyle.SingleUnderline)
            selection.format.setUnderlineColor(QtGui.QColor("#ff8b9e"))
            selections.append(selection)
        self.setExtraSelections(selections)

    def viewportEvent(self, event) -> bool:
        """Show correction text below the pointer; e.g. overlapping grammar and vocabulary evidence can share one tooltip."""

        if event.type() == QtCore.QEvent.Type.ToolTip:
            self._hover(event.pos(), event.globalPos())
            return True
        if event.type() == QtCore.QEvent.Type.Leave:
            QtWidgets.QToolTip.hideText()
        return super().viewportEvent(event)

    def mouseMoveEvent(self, event) -> None:
        """Update hover immediately rather than waiting for the platform tooltip delay; e.g. moving between errors refreshes the explanation."""

        self._hover(event.position().toPoint(), event.globalPosition().toPoint())
        super().mouseMoveEvent(event)

    def _hover(self, point, global_pos) -> None:
        """Map the mouse to saved evidence with proper Unicode offsets; e.g. Dutch diacritics and emoji preserve hit testing."""

        offset = self.cursorForPosition(point).position()
        encoded = self.toPlainText().encode("utf-16-le")[:offset * 2]
        idx = len(encoded.decode("utf-16-le", errors="ignore"))
        matching = [row for row in self._marks if row.start <= idx < row.end]
        show_tip(matching, global_pos, self.viewport())


class QuestionRail(QtWidgets.QListWidget):
    """Provide a narrow clickable question history; e.g. symbols distinguish queued, checking and corrected answers without a large legend."""

    chosen = QtCore.Signal(int)

    def __init__(self) -> None:
        """Keep the overview beside the question; e.g. fifty answers scroll independently of their feedback."""

        super().__init__()
        self.setFixedWidth(82)
        self.setAccessibleName("Sentence question history")
        self.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setStyleSheet("QListWidget {background:#171e29;border:0;border-radius:8px;font-size:12pt;} "
                          "QListWidget::item {padding:9px 4px;border-radius:5px;} "
                          "QListWidget::item:selected {background:#33465e;border:1px solid #93b4db;}")
        self.itemClicked.connect(lambda item: self.chosen.emit(item.data(QtCore.Qt.ItemDataRole.UserRole)))

    def refresh(self, rows: list[dict], current: int | None) -> None:
        """Derive outcomes from saved answers and queue state; e.g. an arrived wrong correction uses a red cross rather than a spinner."""

        scroll = self.verticalScrollBar().value()
        self.clear()
        next_id = next((row["id"] for row in rows if row["answer_text"] is None and row["outcome"] is None), None)
        states = {"correct": ("✓", "#83e5af", "Correction ready · Correct"), "wrong": ("✕", "#ff8b9e", "Correction ready · To revisit"),
                  "invalid": ("−", "#9aa7b7", "Reported"), "checking": ("↻", "#7fdbff", "Correction in progress"),
                  "queued": ("…", "#ffd166", "Correction queued"), "failed": ("!", "#ffb16b", "Correction needs retry")}
        for row in rows:
            state = row["outcome"] or row["state"] or ("queued" if row["answer_text"] is not None else "")
            symbol, color, title = states.get(state, ("○", "#9aa7b7", "Not answered"))
            # A pointer keeps current-question feedback visible without replacing its outcome colour with selection blue.
            pointer = "›" if row["id"] == current else " "
            unseen = row["outcome"] in {"correct", "wrong"} and row["viewed_at"] is None
            item = QtWidgets.QListWidgetItem(f"{pointer} {row['ordinal']:02} {symbol}{'•' if unseen else ''}")
            item.setData(QtCore.Qt.ItemDataRole.UserRole, row["id"])
            item.setForeground(QtGui.QColor(color))
            consulted = " · Not viewed yet" if unseen else (" · Viewed" if row["viewed_at"] is not None else "")
            item.setToolTip(f"Question {row['ordinal']} · {title}{consulted}" + (f"\n{row['error']}" if row["error"] else ""))
            font = item.font()
            font.setBold(unseen)
            item.setFont(font)
            if row["shown_at"] is None and row["id"] != next_id:
                item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEnabled)
            if row.get("session_status") != "active" and row["answer_text"] is None and row["outcome"] is None:
                item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEnabled)
            self.addItem(item)
            if row["id"] == current:
                self.setCurrentItem(item)
        self.verticalScrollBar().setValue(scroll)
