"""Reusable dialogs and summary/detail widgets for the native library.

Example: `AddChannelDialog(parent).values()` returns subscription input.
"""

from __future__ import annotations

from datetime import datetime
from typing import NamedTuple

from PySide6 import QtCore
from PySide6 import QtGui
from PySide6 import QtWidgets

from yt_whisper_subs import cfg
from yt_whisper_subs import library_model
from yt_whisper_subs import library_types as types


class SettingsValues(NamedTuple):
    """Return scheduler and yt-dlp preferences as one cohesive value.

    Example: `SettingsValues(4, "firefox", True)`.
    """

    check_hours: float
    cookies_from_browser: str
    minimize_to_tray: bool


class StatCard(QtWidgets.QFrame):
    """Show one large library count with a compact explanatory label.

    Example: `StatCard("Downloaded")`.
    """

    def __init__(self, title: str) -> None:
        super().__init__()
        self.setObjectName("statCard")
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(1)
        self._value = QtWidgets.QLabel("0")
        self._value.setObjectName("statValue")
        label = QtWidgets.QLabel(title)
        label.setObjectName("statLabel")
        layout.addWidget(self._value)
        layout.addWidget(label)

    def set_value(self, value: int) -> None:
        """Update the prominent numeric count.

        Example: `card.set_value(stats.total)`.
        """

        self._value.setText(f"{value:,}")


class DetailPanel(QtWidgets.QFrame):
    """Present the selected video's useful metadata without table clutter.

    Example: `detail.set_record(record)` updates title and description.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("detailPanel")
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(18, 12, 18, 12)
        self._title = QtWidgets.QLabel("Select a video")
        self._title.setObjectName("detailTitle")
        self._title.setWordWrap(True)
        self._facts = QtWidgets.QLabel("")
        self._facts.setObjectName("detailFacts")
        self._facts.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        self._description = QtWidgets.QLabel("")
        self._description.setWordWrap(True)
        self._description.setMaximumHeight(52)
        self._description.setObjectName("detailDescription")
        layout.addWidget(self._title)
        layout.addWidget(self._facts)
        layout.addWidget(self._description)

    def set_record(self, record: types.VideoRecord | None) -> None:
        """Render a selected record or reset to the empty state.

        Example: `detail.set_record(None)` clears stale selection.
        """

        if not record:
            self._title.setText("Select a video")
            self._facts.clear()
            self._description.clear()
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
