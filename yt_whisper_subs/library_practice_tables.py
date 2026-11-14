"""Shared sortable native learning tables with typed comparison values.

Example: an accuracy of 100 sorts after 75, and formatted dates retain their real timestamp order.
"""

from __future__ import annotations

from PySide6 import QtCore
from PySide6 import QtWidgets


class MetricItem(QtWidgets.QTableWidgetItem):
    """Separate human labels from comparable values; e.g. missing numeric observations sort below measured scores."""

    def __init__(self, label, value) -> None:
        """Retain a sort key without displaying implementation details; e.g. a local date uses a UTC timestamp key."""

        super().__init__()
        self.setData(QtCore.Qt.ItemDataRole.DisplayRole, label)
        self.setData(QtCore.Qt.ItemDataRole.UserRole, value)

    def __lt__(self, other: QtWidgets.QTableWidgetItem) -> bool:
        """Compare consistently typed column values; e.g. numeric counts never sort lexicographically."""

        return self.data(QtCore.Qt.ItemDataRole.UserRole) < other.data(QtCore.Qt.ItemDataRole.UserRole)


class StatsTables(QtWidgets.QTabWidget):
    """Own common read-only table behavior; e.g. verb and sentence analytics share sorting and full hover text."""

    def __init__(self, columns: dict[str, list[str]]) -> None:
        """Create each named view from its domain headers; e.g. word, form and context tables use the same native interactions."""

        super().__init__()
        self.tables = {}
        for name, headers in columns.items():
            table = QtWidgets.QTableWidget(0, len(headers))
            table.setHorizontalHeaderLabels(headers)
            table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
            table.setAlternatingRowColors(True)
            table.verticalHeader().hide()
            table.horizontalHeader().setStretchLastSection(True)
            self.tables[name] = table
            self.addTab(table, name)

    def fill(self, name: str, rows: list[list]) -> None:
        """Normalize labels and date/numeric keys once; e.g. missing textual cells never collide with numeric sentinel types."""

        table = self.tables[name]
        numeric = set()
        for row in rows:
            for col, value in enumerate(row):
                key = value[1] if isinstance(value, tuple) else value
                if isinstance(key, (int, float)):
                    numeric.add(col)
        table.setSortingEnabled(False)
        table.setRowCount(len(rows))
        for idx, row in enumerate(rows):
            for col, value in enumerate(row):
                label, key = value if isinstance(value, tuple) else ("—" if value is None else str(value), value)
                if label == "—" or key is None:
                    key = float("-inf") if col in numeric else ""
                item = MetricItem(label, key)
                item.setToolTip(str(label))
                table.setItem(idx, col, item)
        table.resizeColumnsToContents()
        for col in range(table.columnCount()):
            table.setColumnWidth(col, min(320, table.columnWidth(col)))
        table.setSortingEnabled(True)
