"""Group direct practice navigation separately from activity and learning statistics.

Example: Word quiz, Verbs and Sentences share one labelled group without adding an extra navigation step.
"""

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
            label = QtWidgets.QLabel(title.upper())
            label.setStyleSheet("color:#9cacc1;font-size:9pt;font-weight:600;padding-left:3px;")
            group.addWidget(label)
            buttons = QtWidgets.QHBoxLayout()
            buttons.setSpacing(5)
            for idx in indices:
                button = QtWidgets.QPushButton(tabs.tabText(idx))
                button.setObjectName("practicePageButton")
                button.setAccessibleName(f"{tabs.tabText(idx)} · {title}")
                button.setCheckable(True)
                button.setAutoDefault(False)
                button.setMinimumHeight(32)
                self._buttons.addButton(button, int(idx))
                buttons.addWidget(button)
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
