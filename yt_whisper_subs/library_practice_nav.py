"""Group direct practice navigation separately from activity and learning statistics.

Example: Word quiz, Verbs and Sentences share one labelled group without adding an extra navigation step.
"""

import textwrap

from PySide6 import QtCore
from PySide6 import QtGui
from PySide6 import QtWidgets

from yt_whisper_subs import i18n
from yt_whisper_subs import library_theme
from yt_whisper_subs import practice_activity as activity


class _ModeButton(QtWidgets.QPushButton):
    """Keep daily effort on the right and the guided resume marker on the left of an exercise."""

    def __init__(self, caption: str, title: str, parent: QtWidgets.QWidget) -> None:
        """Store the full destination name for tooltips, e.g. Word quiz in either direction."""

        super().__init__(caption, parent)
        self.setProperty('exercise', True)
        self._title = title
        self._status = activity.DayStatus.UNTRIED
        self._resume = False

    def mark(self, status: activity.DayStatus, resume: bool) -> None:
        """Expose the same meaning to keyboard and screen-reader users, including when both indicators apply."""

        self._status, self._resume = status, resume
        description = {
            activity.DayStatus.UNTRIED: i18n.tr('Not tried today'),
            activity.DayStatus.STARTED: i18n.tr('Started today'),
            activity.DayStatus.COMPLETED: i18n.tr('Completed today'),
        }[status]
        if resume:
            description += '\n' + i18n.tr('Full practice resumes here')
        self.setAccessibleDescription(description)
        self.setToolTip(f'{self._title}\n\n{description}')
        self.update()

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        """Paint compact symbols independent of the row's selection highlight."""

        super().paintEvent(event)
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        x, y = self.width() - 14, self.height() / 2
        point = QtCore.QPointF
        if self._resume:
            painter.setPen(QtGui.QPen(QtGui.QColor('#8fc7ff'), 2))
            painter.drawPolyline([point(9, y - 4), point(13, y), point(9, y + 4)])
        if self._status is activity.DayStatus.COMPLETED:
            painter.setPen(QtGui.QPen(QtGui.QColor('#75dba4'), 2))
            painter.drawPolyline([point(x - 5, y), point(x - 1, y + 4), point(x + 5, y - 4)])
            return
        started = self._status is activity.DayStatus.STARTED
        color = QtGui.QColor('#f1c75b' if started else '#77879b')
        painter.setPen(QtGui.QPen(color, 1.4))
        circle = QtCore.QRectF(x - 5, y - 5, 10, 10)
        painter.drawEllipse(circle)
        if started:
            painter.setBrush(color)
            painter.drawPie(circle, 90 * 16, 180 * 16)  # Qt measures angles in sixteenths of a degree.


class PracticeNav(QtWidgets.QScrollArea):
    """Keep exercise destinations in a scrollable sidebar, e.g. Ctrl+Tab keeps its selection in sync."""

    def __init__(self, tabs: QtWidgets.QTabWidget, groups: tuple, short_labels: dict) -> None:
        """Build one exclusive set of page buttons; e.g. selecting Tracking cannot leave a Practice button selected too."""

        super().__init__()
        self._buttons = QtWidgets.QButtonGroup(self)
        self._modes: dict[int, _ModeButton] = {}
        self._buttons.setExclusive(True)
        self._buttons.idClicked.connect(tabs.setCurrentIndex)
        tabs.currentChanged.connect(self._selected)
        tabs.tabBar().hide()
        self.setObjectName('practiceNav')
        self.setFixedWidth(library_theme.SIDEBAR_WIDTH)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        sidebar = QtWidgets.QWidget()
        sidebar.setObjectName('practiceNavItems')
        self.setWidget(sidebar)
        layout = QtWidgets.QVBoxLayout(sidebar)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(3)
        for group_idx, (title, indices) in enumerate(groups):
            if group_idx:
                layout.addSpacing(10)
            label = QtWidgets.QLabel(title.upper())
            label.setStyleSheet("color:#9cacc1;font-size:9pt;font-weight:600;padding-left:9px;")
            if group_idx == 0:
                heading = QtWidgets.QHBoxLayout()
                heading.addWidget(label, 1)
                today = QtWidgets.QLabel(i18n.tr('Today'))
                today.setStyleSheet('color:#9cacc1;font-size:9pt;padding-right:6px;')
                heading.addWidget(today)
                layout.addLayout(heading)
            else:
                layout.addWidget(label)
            for idx in indices:
                title_text = tabs.tabText(idx)
                caption = short_labels.get(idx, title_text)
                caption = textwrap.fill(caption, width=23, break_long_words=False)
                if idx in short_labels:
                    button = self._modes[idx] = _ModeButton(caption, title_text, sidebar)
                else:
                    button = QtWidgets.QPushButton(caption, sidebar)
                    button.setToolTip(title_text)
                button.setObjectName("practicePageButton")
                button.setAccessibleName(f"{title_text} · {title}")
                button.setCheckable(True)
                button.setAutoDefault(False)
                button.ensurePolished()
                lines = caption.count('\n') + 1
                button.setMinimumHeight(max(28, lines * button.fontMetrics().lineSpacing() + 8))
                self._buttons.addButton(button, int(idx))
                layout.addWidget(button)
        layout.addStretch()
        self._selected(tabs.currentIndex())

    def mark(self, statuses: dict[int, activity.DayStatus], resume_idx: int | None) -> None:
        """Update exercise markers without moving the selection; e.g. paused Full practice can point at another row."""

        for idx, button in self._modes.items():
            button.mark(statuses.get(idx, activity.DayStatus.UNTRIED), idx == resume_idx)

    @QtCore.Slot(int)
    def _selected(self, idx: int) -> None:
        """Mirror native page changes in the grouped navigation; e.g. opening Activity from a reminder selects its visible button."""

        if button := self._buttons.button(idx):
            button.setChecked(True)
            self.ensureWidgetVisible(button, 0, 8)
