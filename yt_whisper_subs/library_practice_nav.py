"""Group direct practice navigation separately from activity and learning statistics.

Example: Word quiz, Verbs and Sentences share one labelled group without adding an extra navigation step.
"""

import textwrap

from PySide6 import QtCore
from PySide6 import QtWidgets

from yt_whisper_subs import library_theme


class PracticeNav(QtWidgets.QScrollArea):
    """Keep exercise destinations in a scrollable sidebar, e.g. Ctrl+Tab keeps its selection in sync."""

    def __init__(self, tabs: QtWidgets.QTabWidget, groups: tuple, short_labels: dict) -> None:
        """Build one exclusive set of page buttons; e.g. selecting Tracking cannot leave a Practice button selected too."""

        super().__init__()
        self._buttons = QtWidgets.QButtonGroup(self)
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
            layout.addWidget(label)
            for idx in indices:
                title_text = tabs.tabText(idx)
                caption = short_labels.get(idx, title_text)
                caption = textwrap.fill(caption, width=28, break_long_words=False)
                button = QtWidgets.QPushButton(caption, sidebar)
                button.setObjectName("practicePageButton")
                button.setAccessibleName(f"{title_text} · {title}")
                button.setCheckable(True)
                button.setAutoDefault(False)
                button.ensurePolished()
                lines = caption.count('\n') + 1
                button.setMinimumHeight(max(28, lines * button.fontMetrics().lineSpacing() + 8))
                button.setToolTip(title_text)
                self._buttons.addButton(button, int(idx))
                layout.addWidget(button)
        layout.addStretch()
        self._selected(tabs.currentIndex())

    @QtCore.Slot(int)
    def _selected(self, idx: int) -> None:
        """Mirror native page changes in the grouped navigation; e.g. opening Activity from a reminder selects its visible button."""

        if button := self._buttons.button(idx):
            button.setChecked(True)
            self.ensureWidgetVisible(button, 0, 8)
