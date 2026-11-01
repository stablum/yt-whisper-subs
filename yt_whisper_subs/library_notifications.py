"""Collect channel discoveries into one quiet, scrollable tray balloon.

Example: `notifier.add(videos)` merges a completed check without per-video alerts.
"""

from __future__ import annotations

import html
from typing import NamedTuple

from PySide6 import QtCore
from PySide6 import QtGui
from PySide6 import QtWidgets

from yt_whisper_subs import library_db
from yt_whisper_subs import library_theme
from yt_whisper_subs import library_types as types


class NotificationPrefs(NamedTuple):
    """Keep notification controls and their persisted defaults together.

    Example: `NotificationPrefs(enabled=False)` silences future discoveries.
    """

    enabled: bool = True
    delay_seconds: int = 30
    duration_seconds: int = 20
    only_hidden: bool = False

    @classmethod
    def load(cls, db: library_db.LibraryDb) -> NotificationPrefs:
        """Validate saved controls before they reach timers or widgets.

        Example: an invalid saved delay falls back to thirty seconds.
        """

        defaults = cls()
        values = []
        for name, default in defaults._asdict().items():
            raw = db.setting(f"notify_{name}", str(int(default)))
            if isinstance(default, bool):
                values.append(raw in {"1", "True", "true"})
                continue
            maximum = 3600 if name == "delay_seconds" else 600
            try:
                value = max(0, min(maximum, int(raw)))
            except ValueError:
                value = default
            values.append(value)
        return cls(*values)

    def save(self, db: library_db.LibraryDb) -> None:
        """Use the library's existing settings store for every control.

        Example: saving persists across a quiet tray restart.
        """

        for name, value in self._asdict().items():
            db.set_setting(f"notify_{name}", int(value))


class NotificationSettings(QtWidgets.QGroupBox):
    """Keep notification editing beside its policy instead of growing the main form.

    Example: `controls.values()` returns the enabled state and both timer durations.
    """

    def __init__(self, prefs: NotificationPrefs) -> None:
        """Expose enablement, aggregation, lifetime, and foreground suppression.

        Example: disabling the feature also disables its tuning controls.
        """

        super().__init__("Pinned-channel notifications")
        self.setObjectName("notificationSettings")
        form = QtWidgets.QFormLayout(self)
        self._enabled = QtWidgets.QCheckBox("Notify me about new videos")
        self._enabled.setObjectName("notifyEnabled")
        self._enabled.setChecked(prefs.enabled)
        self._delay = QtWidgets.QSpinBox()
        self._delay.setObjectName("notifyDelay")
        self._delay.setRange(0, 3600)
        self._delay.setSuffix(" seconds")
        self._delay.setValue(prefs.delay_seconds)
        self._delay.setToolTip(
            "Collect completed checks for this long, then show one list. "
            "Each check always groups all its channels; 0 shows it immediately."
        )
        self._duration = QtWidgets.QSpinBox()
        self._duration.setObjectName("notifyDuration")
        self._duration.setRange(0, 600)
        self._duration.setSuffix(" seconds")
        self._duration.setSpecialValueText("Until dismissed")
        self._duration.setValue(prefs.duration_seconds)
        self._duration.setToolTip("Reading time pauses while the pointer is over the balloon.")
        self._hidden = QtWidgets.QCheckBox("Only while hidden or minimized")
        self._hidden.setObjectName("notifyOnlyHidden")
        self._hidden.setChecked(prefs.only_hidden)
        for control in (self._delay, self._duration, self._hidden):
            control.setEnabled(prefs.enabled)
            self._enabled.toggled.connect(control.setEnabled)
        form.addRow(self._enabled)
        form.addRow("Group arrivals for", self._delay)
        form.addRow("Dismiss after", self._duration)
        form.addRow(self._hidden)

    def values(self) -> NotificationPrefs:
        """Return one cohesive value for saving and immediate runtime updates.

        Example: zero dismissal seconds keeps the balloon open until closed.
        """

        return NotificationPrefs(
            self._enabled.isChecked(), self._delay.value(),
            self._duration.value(), self._hidden.isChecked(),
        )


def popup_rect(anchor: QtCore.QRect, available: QtCore.QRect, size: QtCore.QSize) -> QtCore.QRect:
    """Keep a balloon beside the tray and inside its monitor's usable area.

    Example: a top taskbar places the balloon below its icon.
    """

    width = min(size.width(), available.width())
    height = min(size.height(), available.height())
    x = available.right() - width - 8
    y = available.bottom() - height - 8
    if anchor.isValid():
        x = anchor.center().x() - width // 2
        y = anchor.top() - height - 8
        if y < available.top():
            y = anchor.bottom() + 8
    x = max(available.left(), min(x, available.right() - width + 1))
    y = max(available.top(), min(y, available.bottom() - height + 1))
    return QtCore.QRect(x, y, width, height)


class VideoBalloon(QtWidgets.QDialog):
    """Display all titles by channel without stealing the current app's focus.

    Example: twenty uploads remain readable in one scrollable list.
    """

    video_requested = QtCore.Signal(str)
    settings_requested = QtCore.Signal()
    dismissed = QtCore.Signal()

    def __init__(self, parent: QtWidgets.QWidget) -> None:
        """Build a reusable nonmodal window with explicit dismissal controls.

        Example: Escape or the close button hides the same balloon.
        """

        flags = QtCore.Qt.WindowType.Tool | QtCore.Qt.WindowType.FramelessWindowHint
        flags |= QtCore.Qt.WindowType.WindowStaysOnTopHint
        super().__init__(parent, flags)
        self.setObjectName("videoBalloon")
        self.setWindowTitle("New videos from pinned channels")
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setStyleSheet(library_theme.STYLE)
        self._duration_ms = 0
        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        heading = QtWidgets.QHBoxLayout()
        self._heading = QtWidgets.QLabel()
        self._heading.setObjectName("notificationHeading")
        self._heading.setWordWrap(True)
        close = QtWidgets.QToolButton()
        close.setObjectName("dismissNotification")
        close.setText("×")
        close.setAccessibleName("Dismiss notification")
        close.setToolTip("Dismiss notification (Escape)")
        close.clicked.connect(self.hide)
        heading.addWidget(self._heading, 1)
        heading.addWidget(close)
        self._videos = QtWidgets.QTextBrowser()
        self._videos.setObjectName("notificationVideos")
        self._videos.setOpenLinks(False)
        self._videos.anchorClicked.connect(lambda url: self.video_requested.emit(url.toString()))
        actions = QtWidgets.QHBoxLayout()
        settings = QtWidgets.QPushButton("Settings…")
        settings.clicked.connect(self.settings_requested)
        open_library = QtWidgets.QPushButton("Open library")
        open_library.setObjectName("primaryButton")
        open_library.clicked.connect(lambda: self.video_requested.emit(""))
        actions.addWidget(settings)
        actions.addStretch()
        actions.addWidget(open_library)
        root.addLayout(heading)
        root.addWidget(self._videos, 1)
        root.addLayout(actions)

    def present(self, videos: list[types.VideoMeta], prefs: NotificationPrefs, anchor: QtCore.QRect) -> None:
        """Update the complete list in place and reset its reading time.

        Example: a second check adds titles to an already visible balloon.
        """

        groups: dict[str, list[types.VideoMeta]] = {}
        for meta in videos:
            groups.setdefault(meta.origin.channel, []).append(meta)
        count = len(videos)
        self._heading.setText(f"{count} new video{'s' if count != 1 else ''} · Pinned channels")
        body = []
        for channel, entries in groups.items():
            body.append(f'<p style="color: #f1c75b"><b>{html.escape(channel)}</b></p><ul>')
            for meta in entries:
                video_id = html.escape(meta.identity.video_id, quote=True)
                title = html.escape(meta.identity.title)
                body.append(f'<li><a href="{video_id}" style="color: #b5d9ff">{title}</a></li>')
            body.append("</ul>")
        scroll = self._videos.verticalScrollBar().value()
        self._videos.setHtml("".join(body))
        self._videos.verticalScrollBar().setValue(scroll)
        screen = QtGui.QGuiApplication.screenAt(anchor.center()) if anchor.isValid() else None
        screen = screen or self.parentWidget().screen()
        available = screen.availableGeometry()
        width = min(440, available.width())
        self._videos.document().setTextWidth(max(1, width - 52))
        height = min(440, 132 + int(self._videos.document().size().height()))
        self.setGeometry(popup_rect(anchor, available, QtCore.QSize(width, height)))
        self._duration_ms = prefs.duration_seconds * 1000
        self.show()
        self._restart_timeout()

    def _restart_timeout(self) -> None:
        """Give the reader a full timeout after interaction or a list update.

        Example: zero seconds leaves the balloon open until dismissed.
        """

        self._timer.stop()
        if self._duration_ms and not self.underMouse():
            self._timer.start(self._duration_ms)

    def enterEvent(self, event: QtGui.QEnterEvent) -> None:
        """Pause dismissal while the pointer is reading or scrolling the list.

        Example: hovering keeps a large batch available.
        """

        self._timer.stop()
        super().enterEvent(event)

    def leaveEvent(self, event: QtCore.QEvent) -> None:
        """Resume the full reading timeout after the pointer leaves.

        Example: moving back to another application starts twenty seconds.
        """

        super().leaveEvent(event)
        self._restart_timeout()

    def hideEvent(self, event: QtGui.QHideEvent) -> None:
        """Release the displayed batch on every dismissal path.

        Example: Escape and auto-dismiss both clear accumulated titles.
        """

        self._timer.stop()
        self.dismissed.emit()
        super().hideEvent(event)


class TrayNotifications(QtCore.QObject):
    """Debounce completed discoveries and own at most one visible balloon.

    Example: three channel checks within thirty seconds share one notification.
    """

    video_requested = QtCore.Signal(str)
    settings_requested = QtCore.Signal()

    def __init__(self, db: library_db.LibraryDb, tray: QtWidgets.QSystemTrayIcon) -> None:
        """Keep the queue, preferences, and popup tied to the tray's lifetime.

        Example: `TrayNotifications(service.db, tray)` belongs to its main window.
        """

        super().__init__(tray)
        self._db = db
        self._tray = tray
        self._prefs = NotificationPrefs.load(db)
        self._pending: dict[str, types.VideoMeta] = {}
        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._show_pending)
        self._balloon = VideoBalloon(tray.parent())
        self._balloon.dismissed.connect(self._clear)
        self._balloon.video_requested.connect(self._open_video)
        self._balloon.settings_requested.connect(self._open_settings)
        self._stopped = False

    @QtCore.Slot(object)
    def add(self, videos: list[types.VideoMeta]) -> None:
        """Merge eligible IDs, updating an existing balloon instead of stacking.

        Example: unpinned subscriptions never enter the displayed batch.
        """

        if self._stopped or not self._prefs.enabled:
            return
        for meta in videos:
            self._pending[meta.identity.video_id] = meta
        self._retain_pinned()
        if not self._pending:
            return
        if self._balloon.isVisible():
            self._show_pending()
        elif not self._timer.isActive():
            # A fixed window bounds the wait even if more checks keep arriving.
            self._timer.start(self._prefs.delay_seconds * 1000)

    def configure(self, prefs: NotificationPrefs) -> None:
        """Apply changes immediately and discard alerts when disabled.

        Example: Save with notifications off also closes an existing balloon.
        """

        self._prefs = prefs
        if not prefs.enabled:
            self._balloon.hide()
            self._clear()
        elif self._balloon.isVisible():
            self._show_pending()
        elif self._pending:
            self._timer.start(prefs.delay_seconds * 1000)

    def stop(self) -> None:
        """Prevent late worker signals from reopening notifications during exit.

        Example: quitting while discovery completes keeps the tray quiet.
        """

        self._stopped = True
        self._balloon.hide()
        self._clear()

    def _retain_pinned(self) -> None:
        """Recheck current pin and catalog membership before showing delayed work.

        Example: unpinning during the grouping delay removes that channel's titles.
        """

        pinned = {channel.channel_id for channel in self._db.channels() if channel.pinned}
        self._pending = {
            video_id: meta for video_id, meta in self._pending.items()
            if (record := self._db.video(video_id)) and record.subscription_id in pinned
        }

    @QtCore.Slot()
    def _show_pending(self) -> None:
        """Present one full batch beside the tray after the grouping interval.

        Example: a hidden icon falls back to its main window monitor's corner.
        """

        self._timer.stop()
        self._retain_pinned()
        owner = self._balloon.parentWidget()
        suppressed = self._prefs.only_hidden and owner.isVisible() and not owner.isMinimized()
        if self._stopped or not self._prefs.enabled or suppressed or not self._pending:
            self._balloon.hide()
            self._clear()
            return
        self._balloon.present(list(self._pending.values()), self._prefs, self._tray.geometry())

    def _clear(self) -> None:
        """Consume a displayed or suppressed batch without rescheduling it.

        Example: dismissal cannot produce another balloon for the same batch.
        """

        self._timer.stop()
        self._pending.clear()

    def _open_video(self, video_id: str) -> None:
        """Dismiss before handing a title or general library action to the window.

        Example: clicking an upload selects its catalog row.
        """

        self._balloon.hide()
        self.video_requested.emit(video_id)

    def _open_settings(self) -> None:
        """Make the settings entry reachable directly from the balloon.

        Example: tune the reading time after a large upload batch.
        """

        self._balloon.hide()
        self.settings_requested.emit()
