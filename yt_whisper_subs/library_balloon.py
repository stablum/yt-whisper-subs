"""One tray balloon for new videos and the daily practice reminder.

Example: video discoveries and practice reminders use the same quiet native surface.
"""

from __future__ import annotations

import html
from collections.abc import Collection

from PySide6 import QtCore
from PySide6 import QtGui
from PySide6 import QtWidgets

from yt_whisper_subs import i18n
from yt_whisper_subs import library_practice_reminders as reminders
from yt_whisper_subs import library_theme
from yt_whisper_subs import library_types as types


def popup_rect(anchor: QtCore.QRect, available: QtCore.QRect, size: QtCore.QSize) -> QtCore.QRect:
    """Keep a balloon beside the tray and within its monitor; e.g. support a top taskbar."""

    width, height = min(size.width(), available.width()), min(size.height(), available.height())
    x, y = available.right() - width - 8, available.bottom() - height - 8
    if anchor.isValid():
        x, y = anchor.center().x() - width // 2, anchor.top() - height - 8
        if y < available.top():
            y = anchor.bottom() + 8
    x = max(available.left(), min(x, available.right() - width + 1))
    y = max(available.top(), min(y, available.bottom() - height + 1))
    return QtCore.QRect(x, y, width, height)


class TrayBalloon(QtWidgets.QDialog):
    """Show either or both sections in one focus-safe window beside the tray."""

    dismissed = QtCore.Signal()
    hidden = QtCore.Signal()
    videos_expired = QtCore.Signal()
    video_requested = QtCore.Signal(str)
    practice_requested = QtCore.Signal(bool)
    practice_snoozed = QtCore.Signal()
    settings_requested = QtCore.Signal()

    def __init__(self, parent: QtWidgets.QWidget) -> None:
        """Build the common heading and close control; e.g. Escape follows the same dismissal path."""

        flags = QtCore.Qt.WindowType.Tool | QtCore.Qt.WindowType.FramelessWindowHint
        flags |= QtCore.Qt.WindowType.WindowStaysOnTopHint
        super().__init__(parent, flags)
        self.setObjectName("trayBalloon")
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setStyleSheet(library_theme.STYLE)
        self._duration_ms = 0
        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.videos_expired)
        self._root = QtWidgets.QVBoxLayout(self)
        self._root.setContentsMargins(16, 14, 16, 14)
        heading = QtWidgets.QHBoxLayout()
        self._heading = QtWidgets.QLabel()
        self._heading.setObjectName("notificationHeading")
        self._heading.setWordWrap(True)
        self._heading.installEventFilter(self)
        self._close = QtWidgets.QToolButton()
        self._close.setObjectName("dismissNotification")
        self._close.setText("×")
        self._close.clicked.connect(self.reject)
        heading.addWidget(self._heading, 1)
        heading.addWidget(self._close)
        self._root.addLayout(heading)
        self._videos = QtWidgets.QTextBrowser()
        self._videos.setObjectName("notificationVideos")
        self._videos.setOpenLinks(False)
        self._videos.anchorClicked.connect(lambda url: self.video_requested.emit(url.toString()))
        self._videos.setMinimumHeight(54)
        self._root.addWidget(self._videos)
        self._practice: reminders.PracticeNotice | None = None
        actions = QtWidgets.QHBoxLayout()
        settings = QtWidgets.QPushButton(i18n.tr('Settings…'))
        settings.clicked.connect(self.settings_requested)
        self._activity = QtWidgets.QPushButton(i18n.tr('Activity && calendar'))
        self._activity.setObjectName("openPracticeActivity")
        self._activity.clicked.connect(lambda: self.practice_requested.emit(True))
        self._open_library = QtWidgets.QPushButton(i18n.tr('Open library'))
        self._open_library.clicked.connect(lambda: self.video_requested.emit(""))
        actions.addWidget(settings)
        actions.addWidget(self._activity)
        actions.addStretch()
        actions.addWidget(self._open_library)
        self._root.addLayout(actions)

    def present(
        self, videos: Collection[types.VideoMeta], reminder: reminders.Reminder | None,
        anchor: QtCore.QRect, duration: int,
    ) -> None:
        """Update the sections in place; e.g. remove expired titles while keeping practice visible."""

        screen = QtGui.QGuiApplication.screenAt(anchor.center()) if anchor.isValid() else None
        screen = screen or self.parentWidget().screen()
        available = screen.availableGeometry()
        width = min(470 if reminder else 440, available.width())
        height = 405 if reminder else 104
        self._videos.setVisible(bool(videos))
        self._open_library.setVisible(bool(videos))
        self._activity.setVisible(reminder is not None)
        clickable_heading = reminder is not None and not videos
        self._heading.setCursor(QtCore.Qt.CursorShape.PointingHandCursor if clickable_heading else QtCore.Qt.CursorShape.ArrowCursor)
        if videos:
            video_limit = max(54, min(185, available.height() - height - 30)) if reminder else 336
            height += self._render_videos(videos, width, video_limit)
            title = i18n.tr('New videos from pinned channels')
        else:
            title = i18n.tr('Time for practice')
            self._heading.setText(title)
        self._render_practice(reminder, with_videos=bool(videos))
        if reminder and videos:
            height += 30
        self.setWindowTitle(title)
        self.setGeometry(popup_rect(anchor, available, QtCore.QSize(width, height)))
        self._duration_ms = duration * 1000 if videos else 0
        self.show()
        self._restart_timeout()

    def eventFilter(self, watched, event) -> bool:
        """Route a practice-only title click to Full practice while combined video links keep their actions."""

        if (watched is self._heading and event.type() == QtCore.QEvent.Type.MouseButtonRelease
                and event.button() == QtCore.Qt.MouseButton.LeftButton and self._videos.isHidden()
                and self._practice and self._practice.isVisible()):
            self.practice_requested.emit(False)
            return True
        return super().eventFilter(watched, event)

    def _render_videos(self, videos: Collection[types.VideoMeta], width: int, limit: int) -> int:
        """Keep titles grouped and scrollable; e.g. fifty uploads still fit above the practice section."""

        self._heading.setText(i18n.nt(
            '{count} new video · Pinned channels', '{count} new videos · Pinned channels', len(videos),
        ))
        groups: dict[str, list[types.VideoMeta]] = {}
        for meta in videos:
            groups.setdefault(meta.origin.channel, []).append(meta)
        body = []
        for channel, entries in groups.items():
            body.append(f'<p style="color: #f1c75b"><b>{html.escape(channel)}</b></p><ul>')
            for meta in entries:
                video_id = html.escape(meta.identity.video_id, quote=True)
                title = html.escape(meta.identity.title)
                body.append(f'<li><a href="{video_id}" style="color: #b5d9ff">{title}</a></li>')
            body.append("</ul>")
        document = self._videos.document()
        scroll = self._videos.verticalScrollBar().value()
        self._videos.setHtml("".join(body))
        document.setTextWidth(max(1, width - 52))
        height = min(limit, max(54, int(document.size().height()) + 12))
        self._videos.setMaximumHeight(height)
        self._videos.verticalScrollBar().setValue(scroll)
        return height

    def _render_practice(self, reminder: reminders.Reminder | None, *, with_videos: bool) -> None:
        """Load the chart on demand and label the close button with the current snooze action."""

        if reminder is None:
            if self._practice is not None:
                self._practice.hide()
            self._close.setAccessibleName(i18n.tr('Dismiss notification'))
            self._close.setToolTip(i18n.tr('Dismiss notification (Escape)'))
            return
        if self._practice is None:
            self._practice = reminders.PracticeNotice()
            self._practice.snoozed.connect(self.practice_snoozed)
            self._practice.requested.connect(lambda: self.practice_requested.emit(False))
            self._root.insertWidget(2, self._practice, 1)
        self._practice.render(reminder, with_videos=with_videos)
        self._practice.show()
        hours = reminder.snooze_hours
        self._close.setToolTip(i18n.tr('Snooze for {snooze_hours} hours (Escape)', snooze_hours=hours))
        self._close.setAccessibleName(i18n.tr('Snooze practice reminder for {snooze_hours} hours', snooze_hours=hours))

    def _restart_timeout(self) -> None:
        """Pause expiration while reading; e.g. a hovered video list stays open."""

        self._timer.stop()
        if self._duration_ms and not self.underMouse():
            self._timer.start(self._duration_ms)

    def enterEvent(self, event: QtGui.QEnterEvent) -> None:
        """Hold the reading timer on hover; e.g. scroll a long discovery batch."""

        self._timer.stop()
        super().enterEvent(event)

    def leaveEvent(self, event: QtCore.QEvent) -> None:
        """Restore a full reading interval after hover; e.g. return to the video."""

        super().leaveEvent(event)
        self._restart_timeout()

    def reject(self) -> None:
        """Distinguish dismissal from programmatic hiding; e.g. quitting must not snooze practice."""

        self.dismissed.emit()
        super().reject()

    def hideEvent(self, event: QtGui.QHideEvent) -> None:
        """Stop the reading timer when the window hides; e.g. closing cannot replay the alert."""

        self._timer.stop()
        self.hidden.emit()
        super().hideEvent(event)
