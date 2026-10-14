"""Render channel subscriptions with transient, animated check feedback.

Example: `channels.set_checking(channel_id)` marks a worker's current lookup.
"""

from __future__ import annotations

from PySide6 import QtCore
from PySide6 import QtGui
from PySide6 import QtWidgets

from yt_whisper_subs import library_types as types


class ChannelList(QtWidgets.QListWidget):
    """Keep check animation local to sidebar rows without rebuilding the catalog.

    Example: a pinned channel keeps its spinner through a catalog refresh.
    """

    def __init__(self) -> None:
        """Create an idle animation timer that runs only during channel work.

        Example: an idle library does no animation polling.
        """

        super().__init__()
        self._checking: int | None = None
        self._items: dict[int, tuple[QtWidgets.QListWidgetItem, types.Channel]] = {}
        self._angle = 0
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(80)
        self._timer.timeout.connect(self._animate)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def clear(self) -> None:
        """Discard row references while retaining the current worker identity.

        Example: a pin change rebuilds rows without losing Checking feedback.
        """

        self._items.clear()
        super().clear()

    def add_channel(self, channel: types.Channel) -> None:
        """Add one subscription using both durable flags and transient activity.

        Example: automatic-download and warning markers survive a check.
        """

        item = QtWidgets.QListWidgetItem()
        item.setData(QtCore.Qt.ItemDataRole.UserRole, ("channel", channel.channel_id))
        self._items[channel.channel_id] = item, channel
        self.addItem(item)
        self._render(channel.channel_id)

    @QtCore.Slot(object)
    def set_checking(self, channel_id: int | None) -> None:
        """Move the activity marker without changing selection or channel scope.

        Example: `set_checking(None)` restores the row after cancellation.
        """

        prev = self._checking
        self._checking = channel_id
        if channel_id is None:
            self._timer.stop()
        else:
            self._timer.start()
        for changed in {prev, channel_id} - {None}:
            self._render(changed)

    def _render(self, channel_id: int) -> None:
        """Restore durable row styling or add a clearly worded activity state.

        Example: a finished pinned channel returns to its gold title.
        """

        if not (entry := self._items.get(channel_id)):
            return
        item, channel = entry
        checking = channel_id == self._checking
        auto = "⚡ " if channel.auto_download else ""
        error = "  !" if channel.last_error else ""
        title = f"{auto}{channel.title}{error}"
        item.setText(f"{title}\nChecking…" if checking else title)
        item.setIcon(self._spinner() if checking else QtGui.QIcon())
        color = "#8fc7ff" if checking else "#f1c75b" if channel.pinned else "#eef2f7"
        item.setForeground(QtGui.QColor(color))
        tips = [channel.title, channel.url]
        if checking:
            tips.append("Checking channel for new videos…")
        if channel.pinned:
            tips.append("Pinned for quick access")
        if channel.auto_download:
            tips.append("Automatic download enabled")
        if channel.last_error:
            tips.append(channel.last_error)
        item.setToolTip("\n".join(tips))

    def _spinner(self) -> QtGui.QIcon:
        """Paint a small rotating arc without an external image dependency.

        Example: a selected row retains a visible blue activity icon.
        """

        pixmap = QtGui.QPixmap(32, 32)
        pixmap.fill(QtCore.Qt.GlobalColor.transparent)
        painter = QtGui.QPainter(pixmap)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setPen(QtGui.QPen(QtGui.QColor("#8fc7ff"), 4))
        painter.drawArc(QtCore.QRectF(5, 5, 22, 22), -self._angle * 16, 270 * 16)
        painter.end()
        return QtGui.QIcon(pixmap)

    def _animate(self) -> None:
        """Repaint just the current channel's icon on each animation tick.

        Example: the table and its scroll position remain untouched.
        """

        self._angle = (self._angle + 30) % 360
        if entry := self._items.get(self._checking):
            entry[0].setIcon(self._spinner())
