"""Shared tray-popup frame, placement and explicit dismissal behavior.

Example: video discoveries and practice reminders use the same quiet native surface.
"""

from PySide6 import QtCore
from PySide6 import QtGui
from PySide6 import QtWidgets

from yt_whisper_subs import i18n
from yt_whisper_subs import library_theme


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
    """Share focus-safe presentation without coupling notification policies; e.g. persistent practice reminders."""

    dismissed = QtCore.Signal()
    hidden = QtCore.Signal()

    def __init__(self, title: str, parent: QtWidgets.QWidget) -> None:
        """Build the common heading and close control; e.g. Escape follows the same dismissal path."""

        flags = QtCore.Qt.WindowType.Tool | QtCore.Qt.WindowType.FramelessWindowHint
        flags |= QtCore.Qt.WindowType.WindowStaysOnTopHint
        super().__init__(parent, flags)
        self.setWindowTitle(title)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setStyleSheet(library_theme.STYLE)
        self._duration_ms = 0
        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.reject)
        self._root = QtWidgets.QVBoxLayout(self)
        self._root.setContentsMargins(16, 14, 16, 14)
        heading = QtWidgets.QHBoxLayout()
        self._heading = QtWidgets.QLabel(title)
        self._heading.setObjectName("notificationHeading")
        self._heading.setWordWrap(True)
        close = QtWidgets.QToolButton()
        close.setObjectName("dismissNotification")
        close.setText("×")
        close.setAccessibleName(i18n.tr('Dismiss notification'))
        close.setToolTip(i18n.tr('Dismiss notification (Escape)'))
        close.clicked.connect(self.reject)
        heading.addWidget(self._heading, 1)
        heading.addWidget(close)
        self._root.addLayout(heading)

    def _present(self, anchor: QtCore.QRect, size: QtCore.QSize, duration: int = 0) -> None:
        """Position without activating and optionally expire; e.g. zero duration waits for dismissal."""

        screen = QtGui.QGuiApplication.screenAt(anchor.center()) if anchor.isValid() else None
        screen = screen or self.parentWidget().screen()
        self.setGeometry(popup_rect(anchor, screen.availableGeometry(), size))
        self._duration_ms = duration * 1000
        self.show()
        self._restart_timeout()

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
        """Release the shared notification slot on every hide; e.g. another pending balloon may appear."""

        self._timer.stop()
        self.hidden.emit()
        super().hideEvent(event)
