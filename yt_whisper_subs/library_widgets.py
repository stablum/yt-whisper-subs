"""Reusable controls, dialogs, and details for the native library.

Example: `AddChannelDialog(parent).values()` returns subscription input.
"""

from __future__ import annotations

from datetime import datetime
from typing import NamedTuple

from PySide6 import QtCore
from PySide6 import QtGui
from PySide6 import QtWidgets

from yt_whisper_subs import chapters
from yt_whisper_subs import cfg
from yt_whisper_subs import library_model
from yt_whisper_subs import library_types as types
from yt_whisper_subs import openai_chapters


class SettingsValues(NamedTuple):
    """Return scheduler and yt-dlp preferences as one cohesive value.

    Example: `SettingsValues(4, "firefox", True)`.
    """

    check_hours: float
    cookies_from_browser: str
    minimize_to_tray: bool


class SmartFilterBar(QtWidgets.QFrame):
    """Present task-oriented catalog views as counted, exclusive filter chips.

    Example: `bar.set_counts(proxy.facet_counts(), proxy.rowCount(), False)`.
    """

    view_changed = QtCore.Signal(int)
    reset_requested = QtCore.Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("filterBar")
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(11, 9, 11, 9)
        layout.setSpacing(6)
        eyebrow = QtWidgets.QLabel("SHOW")
        eyebrow.setObjectName("filterEyebrow")
        layout.addWidget(eyebrow)

        self._group = QtWidgets.QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons: dict[library_model.VideoView, QtWidgets.QPushButton] = {}
        for spec in library_model.VIDEO_VIEWS:
            button = QtWidgets.QPushButton(spec.label)
            button.setObjectName("filterChip")
            button.setCheckable(True)
            button.setToolTip(spec.tooltip)
            button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            self._group.addButton(button, int(spec.view))
            self._buttons[spec.view] = button
            layout.addWidget(button)
        self._buttons[library_model.VideoView.ALL].setChecked(True)
        self._group.idClicked.connect(self.view_changed.emit)

        layout.addStretch(1)
        self._result = QtWidgets.QLabel("0 videos")
        self._result.setObjectName("filterResult")
        self._clear = QtWidgets.QPushButton("Clear")
        self._clear.setObjectName("clearFilters")
        self._clear.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self._clear.clicked.connect(lambda: self.reset_requested.emit())
        self._clear.hide()
        layout.addWidget(self._result)
        layout.addWidget(self._clear)

    def set_view(self, view: library_model.VideoView) -> None:
        """Synchronize checked chip state without synthesizing a user click.

        Example: startup restores a persisted `VideoView.CONTINUE` selection.
        """

        self._buttons[view].setChecked(True)

    def set_counts(
        self,
        counts: dict[library_model.VideoView, int],
        visible: int,
        search_active: bool,
    ) -> None:
        """Refresh chip badges, availability, result wording, and reset affordance.

        Example: zero-result inactive views become quiet and non-clickable.
        """

        active = library_model.VideoView(self._group.checkedId())
        labels = {spec.view: spec.label for spec in library_model.VIDEO_VIEWS}
        for view, button in self._buttons.items():
            count = counts.get(view, 0)
            button.setText(f"{labels[view]}  {count:,}")
            button.setEnabled(bool(count) or view in {active, library_model.VideoView.ALL})

        available = counts.get(library_model.VideoView.ALL, 0)
        noun = "match" if search_active else "video"
        if visible == available:
            text = f"{visible:,} {noun}{'' if visible == 1 else 's'}"
        else:
            text = f"{visible:,} of {available:,} {noun}{'' if available == 1 else 's'}"
        self._result.setText(text)
        self._clear.setVisible(search_active or active is not library_model.VideoView.ALL)


class DetailPanel(QtWidgets.QFrame):
    """Present selected-video metadata and navigable bilingual chapters.

    Example: `detail.set_record(record, chapter_set)` updates the inspector.
    """

    chapter_activated = QtCore.Signal(float)
    generate_requested = QtCore.Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("detailPanel")
        self.setMinimumHeight(235)
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        info = QtWidgets.QWidget()
        info_layout = QtWidgets.QVBoxLayout(info)
        info_layout.setContentsMargins(18, 14, 18, 14)
        self._title = QtWidgets.QLabel("Select a video")
        self._title.setObjectName("detailTitle")
        self._title.setWordWrap(True)
        self._facts = QtWidgets.QLabel("")
        self._facts.setObjectName("detailFacts")
        self._facts.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        self._description = QtWidgets.QLabel("")
        self._description.setWordWrap(True)
        self._description.setObjectName("detailDescription")
        self._description.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignTop | QtCore.Qt.AlignmentFlag.AlignLeft
        )
        info_layout.addWidget(self._title)
        info_layout.addWidget(self._facts)
        info_layout.addWidget(self._description, 1)

        chapter_panel = QtWidgets.QFrame()
        chapter_panel.setObjectName("chapterPanel")
        chapter_panel.setMinimumWidth(430)
        chapter_layout = QtWidgets.QVBoxLayout(chapter_panel)
        chapter_layout.setContentsMargins(14, 10, 12, 10)
        chapter_layout.setSpacing(5)
        heading_layout = QtWidgets.QHBoxLayout()
        self._chapter_heading = QtWidgets.QLabel("CHAPTERS")
        self._chapter_heading.setObjectName("chapterHeading")
        self._generate = QtWidgets.QPushButton("✦  Generate")
        self._generate.setObjectName("chapterGenerate")
        self._generate.clicked.connect(self.generate_requested.emit)
        heading_layout.addWidget(self._chapter_heading)
        heading_layout.addStretch(1)
        heading_layout.addWidget(self._generate)
        self._chapter_hint = QtWidgets.QLabel("Select a downloaded video")
        self._chapter_hint.setObjectName("chapterHint")
        self._chapters = QtWidgets.QListWidget()
        self._chapters.setObjectName("chapterList")
        self._chapters.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._chapters.itemDoubleClicked.connect(self._activate_chapter)
        chapter_layout.addLayout(heading_layout)
        chapter_layout.addWidget(self._chapter_hint)
        chapter_layout.addWidget(self._chapters, 1)

        layout.addWidget(info, 1)
        layout.addWidget(chapter_panel)
        self._downloaded = False
        self._busy = False
        self._generate.setEnabled(False)

    def set_record(
        self,
        record: types.VideoRecord | None,
        chapter_set: chapters.ChapterSet | None = None,
    ) -> None:
        """Render a selected record, its chapter plan, or the empty state.

        Example: `detail.set_record(None)` clears stale selection.
        """

        self._chapters.clear()
        self._downloaded = bool(record and record.downloaded)
        if not record:
            self._title.setText("Select a video")
            self._facts.clear()
            self._description.clear()
            self._chapter_heading.setText("CHAPTERS")
            self._chapter_hint.setText("Select a downloaded video")
            self._generate.setText("✦  Generate")
            self._update_generate_action()
            return
        meta = record.meta
        self._title.setText(meta.identity.title)
        published = library_model.format_timestamp(meta.origin.published_at)
        facts = [meta.origin.channel, f"Published {published}", f"YouTube ID {meta.identity.video_id}"]
        if record.local:
            facts.append(str(record.local.path))
        if record.download_error:
            facts.append(f"Last error: {record.download_error}")
        self._facts.setText("  ·  ".join(facts))
        self._description.setText(meta.details.description.strip().replace("\n", " ") or meta.identity.url)
        self._render_chapters(chapter_set)

    def set_busy(self, busy: bool) -> None:
        """Keep contextual generation disabled while foreground work runs.

        Example: `detail.set_busy(True)` follows task dispatch.
        """

        self._busy = busy
        self._update_generate_action()

    def _render_chapters(self, chapter_set: chapters.ChapterSet | None) -> None:
        """Populate bilingual jump rows or explain how to create them.

        Example: `_render_chapters(chapter_set)` displays exact timestamps.
        """

        if not self._downloaded:
            self._chapter_heading.setText("CHAPTERS")
            self._chapter_hint.setText("Download this video to create chapters")
            self._generate.setText("✦  Generate")
            self._update_generate_action()
            return
        if not chapter_set:
            self._chapter_heading.setText("CHAPTERS")
            self._chapter_hint.setText("No chapter plan yet · generated from your subtitles")
            self._generate.setText("✦  Generate")
            self._update_generate_action()
            return

        self._chapter_heading.setText(f"CHAPTERS  ·  {len(chapter_set.chapters)}")
        self._chapter_hint.setText("Double-click a chapter to jump to that moment")
        self._generate.setText("↻  Regenerate")
        for chapter in chapter_set.chapters:
            timestamp = openai_chapters.format_time(chapter.start_ms)
            text = f"{timestamp:>7}    {chapter.primary_title}\n          {chapter.english_title}"
            item = QtWidgets.QListWidgetItem(text)
            item.setData(QtCore.Qt.ItemDataRole.UserRole, chapter.start_ms / 1000)
            item.setToolTip(f"Jump to {timestamp}")
            self._chapters.addItem(item)
        self._update_generate_action()

    def _activate_chapter(self, item: QtWidgets.QListWidgetItem) -> None:
        """Emit the trusted local seek time stored on one chapter row.

        Example: double-clicking `12:30` emits `750.0` seconds.
        """

        start_seconds = item.data(QtCore.Qt.ItemDataRole.UserRole)
        if isinstance(start_seconds, (int, float)):
            self.chapter_activated.emit(float(start_seconds))

    def _update_generate_action(self) -> None:
        """Apply download and busy constraints to the contextual AI action.

        Example: `_update_generate_action()` disables remote-only videos.
        """

        self._generate.setEnabled(self._downloaded and not self._busy)


class ActivityTrace(QtWidgets.QDockWidget):
    """Retain timestamped GUI and subprocess activity in an optional dock.

    Example: `trace.append_message("Downloading 25%")` records live progress.
    """

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__("Activity trace", parent)
        self.setObjectName("activityTraceDock")
        self.setAllowedAreas(QtCore.Qt.DockWidgetArea.BottomDockWidgetArea)
        features = (
            QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetClosable
            | QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetFloatable
        )
        self.setFeatures(features)
        panel = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(panel)
        layout.setContentsMargins(10, 8, 10, 10)
        layout.setSpacing(6)
        toolbar = QtWidgets.QHBoxLayout()
        note = QtWidgets.QLabel("Live output is retained while this panel is hidden.")
        note.setObjectName("traceNote")
        copy = QtWidgets.QPushButton("Copy all")
        clear = QtWidgets.QPushButton("Clear")
        toolbar.addWidget(note, 1)
        toolbar.addWidget(copy)
        toolbar.addWidget(clear)
        self._output = QtWidgets.QPlainTextEdit()
        self._output.setObjectName("activityTrace")
        self._output.setReadOnly(True)
        self._output.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.NoWrap)
        self._output.document().setMaximumBlockCount(cfg.DEFAULT_LIBRARY_TRACE_LINES)
        copy.clicked.connect(self._copy_all)
        clear.clicked.connect(self._output.clear)
        layout.addLayout(toolbar)
        layout.addWidget(self._output, 1)
        self.setWidget(panel)

    @QtCore.Slot(str)
    def append_message(self, message: str) -> None:
        """Append non-empty lines with one local timestamp and auto-scroll.

        Example: `append_message("Whisper started")` adds a dated trace row.
        """

        lines = message.replace("\r\n", "\n").replace("\r", "\n").splitlines()
        lines = [line.rstrip() for line in lines if line.strip()]
        if not lines:
            return
        timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
        text = "\n".join(f"[{timestamp}] {line}" for line in lines)
        self._output.appendPlainText(text)
        scroll_bar = self._output.verticalScrollBar()
        scroll_bar.setValue(scroll_bar.maximum())

    def _copy_all(self) -> None:
        """Copy the complete retained trace without changing selection state.

        Example: the Copy all button invokes `_copy_all()`.
        """

        QtGui.QGuiApplication.clipboard().setText(self._output.toPlainText())


class AddChannelDialog(QtWidgets.QDialog):
    """Collect one channel handle/URL and its future auto-download policy.

    Example: `dialog.values()` returns `(url, auto_download)`.
    """

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Track a YouTube channel")
        self.setMinimumWidth(500)
        layout = QtWidgets.QVBoxLayout(self)
        heading = QtWidgets.QLabel("Add a channel")
        heading.setObjectName("dialogHeading")
        explanation = QtWidgets.QLabel(
            "Enter an @handle or channel URL. Existing uploads become browsable; "
            "automatic download starts only for videos discovered by later checks."
        )
        explanation.setWordWrap(True)
        self._url = QtWidgets.QLineEdit()
        self._url.setPlaceholderText("@channel or https://www.youtube.com/@channel")
        self._auto = QtWidgets.QCheckBox("Automatically download future videos")
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Cancel | QtWidgets.QDialogButtonBox.StandardButton.Ok
        )
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        layout.addWidget(heading)
        layout.addWidget(explanation)
        layout.addSpacing(8)
        layout.addWidget(self._url)
        layout.addWidget(self._auto)
        layout.addWidget(buttons)
        self._url.setFocus()

    def values(self) -> tuple[str, bool]:
        """Return normalized whitespace and the requested automation flag.

        Example: `url, auto = dialog.values()` after acceptance.
        """

        return self._url.text().strip(), self._auto.isChecked()

    def _accept_if_valid(self) -> None:
        """Keep the dialog open until a non-empty channel value is supplied.

        Example: the OK button invokes `_accept_if_valid()`.
        """

        if not self._url.text().strip():
            self._url.setFocus()
            return
        self.accept()


class SettingsDialog(QtWidgets.QDialog):
    """Edit scheduling, browser cookies, and tray lifetime preferences.

    Example: `dialog.values()` supplies durable settings after acceptance.
    """

    def __init__(self, values: SettingsValues, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Library settings")
        self.setMinimumWidth(460)
        layout = QtWidgets.QVBoxLayout(self)
        heading = QtWidgets.QLabel("Library settings")
        heading.setObjectName("dialogHeading")
        form = QtWidgets.QFormLayout()
        self._hours = QtWidgets.QDoubleSpinBox()
        self._hours.setRange(0.25, 168)
        self._hours.setDecimals(2)
        self._hours.setSuffix(" hours")
        self._hours.setValue(values.check_hours)
        self._cookies = QtWidgets.QComboBox()
        self._cookies.setEditable(True)
        self._cookies.addItems(["", "firefox", "chrome", "edge", "brave"])
        self._cookies.setCurrentText(values.cookies_from_browser)
        self._tray = QtWidgets.QCheckBox("Keep checking when the window is closed")
        self._tray.setChecked(values.minimize_to_tray)
        form.addRow("Check every", self._hours)
        form.addRow("Cookies from browser", self._cookies)
        form.addRow("Background", self._tray)
        note = QtWidgets.QLabel(
            "Scheduled checks run while the app is open or living in the system tray. "
            "The first subscription check establishes history and never downloads a channel backlog."
        )
        note.setWordWrap(True)
        note.setObjectName("settingsNote")
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Cancel | QtWidgets.QDialogButtonBox.StandardButton.Save
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(heading)
        layout.addLayout(form)
        layout.addWidget(note)
        layout.addWidget(buttons)

    def values(self) -> SettingsValues:
        """Return all edited settings as one compositional value.

        Example: `settings = dialog.values()` after Save.
        """

        return SettingsValues(
            self._hours.value(),
            self._cookies.currentText().strip(),
            self._tray.isChecked(),
        )
