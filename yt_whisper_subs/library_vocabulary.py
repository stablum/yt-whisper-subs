"""Native vocabulary browsing with occurrence history and contextual translations.

Example: the Vocabulary tab shows clicks grouped by spelling or dictionary form.
"""

from __future__ import annotations

import json
from datetime import datetime

from PySide6 import QtCore
from PySide6 import QtGui
from PySide6 import QtWidgets

from yt_whisper_subs import i18n
from yt_whisper_subs import library_service
from yt_whisper_subs import openai_chapters


def _when(timestamp: float) -> str:
    """Display stored UTC instants in local time; e.g. distinguish two clicks today."""

    return i18n.date_text(datetime.fromtimestamp(timestamp).astimezone(), '%Y-%m-%d %H:%M:%S')


def _time(seconds: float) -> str:
    """Reuse the chapter timestamp formatter; e.g. an occurrence at `08:55`."""

    return openai_chapters.format_time(round(seconds * 1000))


class VocabularyView(QtWidgets.QWidget):
    """Keep derived counts and immutable history in one review surface; e.g. repeated `liep`."""

    play_requested = QtCore.Signal(object)

    def __init__(self, service: library_service.LibraryService, parent: QtWidgets.QWidget | None = None) -> None:
        """Bind the review page to one output root; e.g. the currently open library."""

        super().__init__(parent)
        self._service = service
        self._snapshot = None
        self._events: list[dict] = []
        self._search = QtWidgets.QLineEdit()
        self._search.setPlaceholderText(i18n.tr('Search words, meanings, or dictionary forms…'))
        self._search.setClearButtonEnabled(True)
        self._group = QtWidgets.QCheckBox(i18n.tr('Group by dictionary form'))
        self._status = QtWidgets.QLabel()
        self._words = self._table([i18n.tr('Word'), i18n.tr('Language'), i18n.tr('Dictionary form'), i18n.tr('Meaning'), i18n.tr('Clicks'), i18n.tr('Mistakes'), i18n.tr('Videos'), i18n.tr('Compounds'), i18n.tr('Last seen'), i18n.tr('Translation')])
        self._history = self._table([i18n.tr('Recorded at'), i18n.tr('Source'), i18n.tr('Cue time'), i18n.tr('Click position'), i18n.tr('Meaning'), i18n.tr('Translation')])
        for col, width in enumerate((125, 75, 135, 180, 60, 70, 65, 95, 160, 105)):
            self._words.setColumnWidth(col, width)
        for col, width in enumerate((160, 245, 90, 105, 200, 105)):
            self._history.setColumnWidth(col, width)
        self._detail = QtWidgets.QPlainTextEdit()
        self._detail.setObjectName("vocabularyDetail")
        self._detail.setReadOnly(True)
        self._detail.setPlaceholderText(i18n.tr('Select a word and an occurrence to inspect its original sentence and meaning.'))
        self._arrange()
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(2000)
        self._timer.timeout.connect(self.refresh)

    def _arrange(self) -> None:
        """Compose resizable panels and their native actions; e.g. replay a selected occurrence."""

        retry = QtWidgets.QPushButton(i18n.tr('Retry failed lookups / enrichment'))
        play = QtWidgets.QPushButton(i18n.tr('Play occurrence'))
        source = QtWidgets.QPushButton(i18n.tr('Open source'))
        self._play, self._source = play, source
        top = QtWidgets.QHBoxLayout()
        top.addWidget(self._search, 1)
        top.addWidget(self._group)
        actions = QtWidgets.QHBoxLayout()
        actions.addWidget(play)
        actions.addWidget(source)
        actions.addStretch()
        actions.addWidget(retry)
        split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        for widget in (self._words, self._history, self._detail):
            split.addWidget(widget)
        split.setSizes([340, 180, 170])
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 16)
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

    def showEvent(self, event: QtGui.QShowEvent) -> None:
        """Catch up on new words and translations when the user returns to the tab."""

        super().showEvent(event)
        self.refresh()
        self._timer.start()

    def hideEvent(self, event: QtGui.QHideEvent) -> None:
        """Pause table polling while the page is hidden; background lookups keep running."""

        self._timer.stop()
        super().hideEvent(event)

    def focus_search(self) -> None:
        """Focus the tab's filter field so Ctrl+F can replace the current word query."""

        self._search.setFocus()
        self._search.selectAll()

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
        enrichment = self._service.db.accretion_status()
        snapshot = (rows, query, self._group.isChecked(), enrichment)
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
            status = f"{row['failed']} failed" if row["failed"] else (f"{row['pending']} pending" if row["pending"] else i18n.tr('Ready'))
            values = [word, row["language"], row["lemma"] or "", row["meaning"] or "",
                      row["clicks"], row["mistakes"], row["videos"], row["components"], _when(row["occurred_at"]), status]
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
        pending = sum(count for state, count in enrichment.items() if state not in {"done", "failed"})
        failed = enrichment.get("failed", 0)
        self._status.setText(i18n.tr(
            '{len:,} words · {sum:,} clicks · {sum3:,} word confusions · enrichment: {pending} '
            'pending, {failed} failed',
            len=len(rows),
            sum=sum((row['clicks'] for row in rows)),
            sum3=sum((row['mistakes'] for row in rows)),
            pending=pending,
            failed=failed,
        ))
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
            cue = _time(row["cue_start_ms"] / 1000) if row["cue_start_ms"] is not None else "—"
            position = _time(row["position_seconds"]) if row["position_seconds"] is not None else "—"
            source = row["title"] + (f" · {row['role']}" if row["role"] in {"intended", "confused"} else "")
            values = [_when(row["occurred_at"]), source, cue, position, row["meaning"] or "", row["state"].capitalize()]
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
        playable = row is not None and row["origin"] == "click"
        self._play.setEnabled(playable)
        self._source.setEnabled(playable)
        if row is None:
            self._detail.clear()
            return
        ctx = json.loads(row["context_json"])
        lines = [f"{row['surface']} → {row['meaning'] or i18n.tr('Translation pending')}",
                 i18n.tr('Dictionary form: {value} · {value2}', value=row['lemma'] or '—', value2=row['lemma_meaning'] or '—'),
                 f"{row['part_of_speech'] or ''} · {row['note'] or ''}",
                 i18n.tr('Expression: {value}', value=row['expression'] or '—'), "", ctx["sentence"]]
        for split in self._service.db.word_compounds(row["lookup_id"]):
            parts = []
            for part in split.parts:
                if part.word is None:
                    parts.append(i18n.tr('{text} (linking element)', text=part.text))
                else:
                    parts.append(f"{part.text} [{part.word.lemma}: {part.word.meaning}]")
            lines.extend(["", i18n.tr('Compound: {lemma} = {value}', lemma=split.lemma, value=' + '.join(parts))])
        if row["origin"] == "click":
            lines.extend(["", i18n.tr('Before: {before}', before=ctx['before']), i18n.tr('After: {after}', after=ctx['after']),
                          i18n.tr(
                              'English subtitle: {value}',
                              value=ctx['english'] or '—',
                          ), "", i18n.tr(
                              'Source: {url}',
                              url=row['url'],
                          ), i18n.tr(
                              'File: {path}',
                              path=row['path'],
                          )])
        elif row["origin"] == "component":
            lines.extend(["", row["title"], i18n.tr('Added as an unassessed constituent; no independent mistake was recorded.')])
        else:
            lines.extend(["", i18n.tr('{title} · {role} concept', title=row['title'], role=row['role']),
                          i18n.tr('You selected or typed: {value}', value=row['typed'] or '(omitted)'), i18n.tr('Correction: {correction}', correction=row['correction']),
                          i18n.tr('English: {value}', value=ctx['english'] or '—')])
        if row["ambiguous"]:
            lines.append(i18n.tr('Meaning is ambiguous in this context.'))
        if row["error"]:
            lines.append(i18n.tr('Translation error: {error}', error=row['error']))
        self._detail.setPlainText("\n".join(lines))

    def _play_occurrence(self, *_args: object) -> None:
        """Delegate playback to the library's existing worker; e.g. double-click a history row."""

        if (row := self._selected_event()) and row["origin"] == "click":
            self.play_requested.emit(row)

    def _open_source(self) -> None:
        """Open the original URL or local file; e.g. a removed download's YouTube page."""

        if (row := self._selected_event()) and row["origin"] == "click":
            QtGui.QDesktopServices.openUrl(QtCore.QUrl(row["url"]))

    def _retry(self) -> None:
        """Reschedule failed lookups while preserving counts; e.g. restored internet access."""

        self._service.vocabulary.retry()
        self.refresh()
