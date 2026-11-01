"""Native vocabulary browsing with occurrence history and contextual translations.

Example: Library → Vocabulary shows clicks grouped by spelling or dictionary form.
"""

from __future__ import annotations

import json
from datetime import datetime

from PySide6 import QtCore
from PySide6 import QtGui
from PySide6 import QtWidgets

from yt_whisper_subs import library_service
from yt_whisper_subs import openai_chapters


def _when(timestamp: float) -> str:
    """Display stored UTC instants in local time; e.g. distinguish two clicks today."""

    return datetime.fromtimestamp(timestamp).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _time(seconds: float) -> str:
    """Reuse the chapter timestamp formatter; e.g. an occurrence at `08:55`."""

    return openai_chapters.format_time(round(seconds * 1000))


class VocabularyDialog(QtWidgets.QDialog):
    """Keep derived counts and immutable history in one review surface; e.g. repeated `liep`."""

    play_requested = QtCore.Signal(object)

    def __init__(self, service: library_service.LibraryService, parent: QtWidgets.QWidget | None = None) -> None:
        """Bind the review window to one output root; e.g. the currently open library."""

        super().__init__(parent)
        self.setWindowTitle("Vocabulary · selected subtitle words")
        self.resize(1080, 760)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        self._service = service
        self._snapshot = None
        self._events: list[dict] = []
        self._search = QtWidgets.QLineEdit()
        self._search.setPlaceholderText("Search words, meanings, or dictionary forms…")
        self._group = QtWidgets.QCheckBox("Group by dictionary form")
        self._status = QtWidgets.QLabel()
        self._words = self._table(["Word", "Language", "Dictionary form", "Meaning here", "Clicks", "Videos", "Last clicked", "Translation"])
        self._history = self._table(["Clicked at", "Video", "Cue time", "Click position", "Meaning here", "Translation"])
        for col, width in enumerate((125, 75, 135, 200, 60, 65, 160, 120)):
            self._words.setColumnWidth(col, width)
        for col, width in enumerate((160, 245, 90, 105, 200, 105)):
            self._history.setColumnWidth(col, width)
        self._detail = QtWidgets.QPlainTextEdit()
        self._detail.setObjectName("vocabularyDetail")
        self._detail.setReadOnly(True)
        self._detail.setPlaceholderText("Select a word and an occurrence to inspect its original sentence and meaning.")
        self._arrange()
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(2000)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self._service.vocabulary.start()
        self.refresh()

    def _arrange(self) -> None:
        """Compose resizable panels and their native actions; e.g. replay a selected occurrence."""

        retry = QtWidgets.QPushButton("Retry failed translations")
        play = QtWidgets.QPushButton("Play occurrence")
        source = QtWidgets.QPushButton("Open source")
        close = QtWidgets.QPushButton("Close")
        self._play, self._source = play, source
        top = QtWidgets.QHBoxLayout()
        top.addWidget(self._search, 1)
        top.addWidget(self._group)
        actions = QtWidgets.QHBoxLayout()
        for button in (play, source, retry):
            actions.addWidget(button)
        actions.addStretch()
        actions.addWidget(close)
        split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        for widget in (self._words, self._history, self._detail):
            split.addWidget(widget)
        split.setSizes([340, 180, 170])
        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self._status)
        layout.addWidget(split, 1)
        layout.addLayout(actions)
        self._search.textChanged.connect(self.refresh)
        self._group.toggled.connect(self.refresh)
        self._words.itemSelectionChanged.connect(self._show_history)
        self._history.itemSelectionChanged.connect(self._show_detail)
        self._history.itemDoubleClicked.connect(self._play_occurrence)
        play.clicked.connect(self._play_occurrence)
        source.clicked.connect(self._open_source)
        retry.clicked.connect(self._retry)
        close.clicked.connect(self.close)

    @staticmethod
    def _table(labels: list[str]) -> QtWidgets.QTableWidget:
        """Use native sortable, selectable rows; e.g. compare click and distinct-video counts."""

        table = QtWidgets.QTableWidget(0, len(labels))
        table.setHorizontalHeaderLabels(labels)
        table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setAlternatingRowColors(True)
        table.setWordWrap(False)
        table.verticalHeader().hide()
        table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Interactive)
        table.horizontalHeader().setStretchLastSection(True)
        return table

    def refresh(self, *_args: object) -> None:
        """Refresh completed translations without losing selection; e.g. an API reply arrives."""

        rows = self._service.db.word_summary(by_lemma=self._group.isChecked())
        query = self._search.text().casefold().strip()
        snapshot = (rows, query, self._group.isChecked())
        if snapshot == self._snapshot:
            return
        self._snapshot = snapshot
        selected = self._words.currentItem()
        key = selected.data(QtCore.Qt.ItemDataRole.UserRole) if selected else None
        self._words.blockSignals(True)
        self._words.setSortingEnabled(False)
        self._words.setRowCount(0)
        for row in rows:
            searchable = " ".join(str(row.get(field) or "") for field in ("surface", "lemma", "meaning", "lemma_meaning"))
            if query and query not in searchable.casefold():
                continue
            word = row["lemma"] if self._group.isChecked() and row["lemma"] else row["surface"]
            status = f"{row['failed']} failed" if row["failed"] else (f"{row['pending']} pending" if row["pending"] else "Ready")
            values = [word, row["language"], row["lemma"] or "", row["meaning"] or "",
                      row["clicks"], row["videos"], _when(row["clicked_at"]), status]
            idx = self._words.rowCount()
            self._words.insertRow(idx)
            identity = (row["language"], row["word_key"])
            for col, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem()
                item.setData(QtCore.Qt.ItemDataRole.DisplayRole, value)
                item.setData(QtCore.Qt.ItemDataRole.UserRole, identity)
                self._words.setItem(idx, col, item)
            if identity == key:
                self._words.selectRow(idx)
        self._words.setSortingEnabled(True)
        self._words.blockSignals(False)
        self._status.setText(f"{len(rows):,} words · {sum(row['clicks'] for row in rows):,} clicks · each click is retained")
        self._show_history()

    def _show_history(self) -> None:
        """List each occurrence separately; e.g. two clicks in one video are two rows."""

        item = self._words.currentItem()
        identity = item.data(QtCore.Qt.ItemDataRole.UserRole) if item else None
        old = self._selected_event()
        self._events = self._service.db.word_history(*identity, by_lemma=self._group.isChecked()) if identity else []
        self._history.blockSignals(True)
        self._history.setRowCount(len(self._events))
        selected = 0
        for idx, row in enumerate(self._events):
            values = [_when(row["clicked_at"]), row["title"], _time(row["cue_start_ms"] / 1000),
                      _time(row["position_seconds"]), row["meaning"] or "", row["state"].capitalize()]
            for col, value in enumerate(values):
                self._history.setItem(idx, col, QtWidgets.QTableWidgetItem(value))
            if old and row["id"] == old["id"]:
                selected = idx
        if self._events:
            self._history.selectRow(selected)
        self._history.blockSignals(False)
        self._show_detail()

    def _selected_event(self) -> dict | None:
        """Resolve the selected immutable event; e.g. replay the correct video after sorting."""

        row = self._history.currentRow()
        return self._events[row] if 0 <= row < len(self._events) else None

    def _show_detail(self) -> None:
        """Show source and translation provenance plainly; e.g. an ambiguous idiom."""

        row = self._selected_event()
        self._play.setEnabled(row is not None)
        self._source.setEnabled(row is not None)
        if row is None:
            self._detail.clear()
            return
        ctx = json.loads(row["context_json"])
        lines = [f"{row['surface']} → {row['meaning'] or 'Translation pending'}",
                 f"Dictionary form: {row['lemma'] or '—'} · {row['lemma_meaning'] or '—'}",
                 f"{row['part_of_speech'] or ''} · {row['note'] or ''}",
                 f"Expression: {row['expression'] or '—'}", "", ctx["sentence"],
                 "", f"Before: {ctx['before']}", f"After: {ctx['after']}",
                 f"English subtitle: {ctx['english'] or '—'}", "",
                 f"Source: {row['url']}", f"File: {row['path']}"]
        if row["ambiguous"]:
            lines.append("Meaning is ambiguous in this context.")
        if row["error"]:
            lines.append(f"Translation error: {row['error']}")
        self._detail.setPlainText("\n".join(lines))

    def _play_occurrence(self, *_args: object) -> None:
        """Delegate playback to the library's existing worker; e.g. double-click a history row."""

        if row := self._selected_event():
            self.play_requested.emit(row)

    def _open_source(self) -> None:
        """Open the original URL or local file; e.g. a removed download's YouTube page."""

        if row := self._selected_event():
            QtGui.QDesktopServices.openUrl(QtCore.QUrl(row["url"]))

    def _retry(self) -> None:
        """Reschedule failed lookups while preserving counts; e.g. restored internet access."""

        self._service.vocabulary.retry()
        self.refresh()
