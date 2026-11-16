"""Group direct practice navigation separately from activity and learning statistics.

Example: Word quiz, Verbs and Sentences share one labelled group without adding an extra navigation step.
"""

import textwrap

from PySide6 import QtCore
from PySide6 import QtWidgets


class PracticeNav(QtWidgets.QWidget):
    """Expose two clear groups while the native tab widget owns page state; e.g. Ctrl+Tab and direct buttons stay synchronized."""

    def __init__(self, tabs: QtWidgets.QTabWidget, groups: tuple) -> None:
        """Build one exclusive set of page buttons; e.g. selecting Tracking cannot leave a Practice button selected too."""

        super().__init__()
        self._buttons = QtWidgets.QButtonGroup(self)
        self._buttons.setExclusive(True)
        self._buttons.idClicked.connect(tabs.setCurrentIndex)
        tabs.currentChanged.connect(self._selected)
        tabs.tabBar().hide()
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 4)
        row.setSpacing(12)
        for group_idx, (title, indices) in enumerate(groups):
            if group_idx:
                divider = QtWidgets.QFrame()
                divider.setFrameShape(QtWidgets.QFrame.Shape.VLine)
                divider.setStyleSheet("color:#465368;margin:0 10px;")
                row.addWidget(divider)
            group = QtWidgets.QVBoxLayout()
            group.setSpacing(5)
            group.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
            label = QtWidgets.QLabel(title.upper())
            label.setStyleSheet("color:#9cacc1;font-size:9pt;font-weight:600;padding-left:3px;")
            group.addWidget(label)
            buttons = QtWidgets.QGridLayout()
            buttons.setSpacing(5)
            columns = 4 if len(indices) > 6 else 3
            for pos, idx in enumerate(indices):
                title_text = tabs.tabText(idx)
                lines = [textwrap.fill(line, width=22, break_long_words=False) for line in title_text.splitlines()]
                button = QtWidgets.QPushButton("\n".join(lines))
                button.setObjectName("practicePageButton")
                button.setAccessibleName(f"{tabs.tabText(idx)} · {title}")
                button.setCheckable(True)
                button.setAutoDefault(False)
                button.setMinimumHeight(64)
                button.setToolTip(title_text)
                self._buttons.addButton(button, int(idx))
                buttons.addWidget(button, pos // columns, pos % columns)
            group.addLayout(buttons)
            row.addLayout(group)
        row.addStretch()
        self.setStyleSheet("QPushButton#practicePageButton {padding:6px 15px;border-radius:6px;} "
                          "QPushButton#practicePageButton:checked {background:#284b70;border-color:#649bd0;color:#ffffff;}")
        self._selected(tabs.currentIndex())

    @QtCore.Slot(int)
    def _selected(self, idx: int) -> None:
        """Mirror native page changes in the grouped navigation; e.g. opening Activity from a reminder selects its visible button."""

        if button := self._buttons.button(idx):
            button.setChecked(True)
