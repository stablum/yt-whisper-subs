"""Qt table model and formatting policy for the native video catalog.

Example: `VideoTableModel().set_records(db.videos())`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from PySide6 import QtCore
from PySide6 import QtGui

from yt_whisper_subs import library_types as types


SORT_ROLE = QtCore.Qt.ItemDataRole.UserRole + 1


def format_timestamp(value: int | None) -> str:
    """Render a local date and time without inventing unavailable metadata.

    Example: `format_timestamp(None)` returns an em dash.
    """

    if value is None:
        return "—"
    return datetime.fromtimestamp(value).astimezone().strftime("%Y-%m-%d  %H:%M")


def format_duration(seconds: float | None) -> str:
    """Render seconds as compact hour/minute/second text.

    Example: `format_duration(65)` returns `1:05`.
    """

    if seconds is None:
        return "—"
    total = max(0, round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02}:{secs:02}" if hours else f"{minutes}:{secs:02}"


def format_size(size_bytes: int | None) -> str:
    """Render file bytes with readable binary units.

    Example: `format_size(1048576)` returns `1.0 MiB`.
    """

    if size_bytes is None:
        return "—"
    size = float(size_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit in {"B", "KiB"} else f"{size:.1f} {unit}"
        size /= 1024
    return "—"


def video_status(record: types.VideoRecord) -> str:
    """Choose the most useful compact status for a table row.

    Example: `video_status(record)` returns `Downloaded` when playable.
    """

    if record.downloaded:
        return "Downloaded"
    if record.download_error:
        return "Error"
    if record.meta.details.live_status == "is_live":
        return "Live now"
    if record.meta.details.live_status == "is_upcoming":
        return "Upcoming"
    return "Available"


class VideoTableModel(QtCore.QAbstractTableModel):
    """Expose typed catalog records as a sortable, read-only Qt table.

    Example: `model.record(index.row())` returns the selected video.
    """

    _columns = (
        ("Status", 110),
        ("Title", 420),
        ("Channel", 190),
        ("Published", 145),
        ("Downloaded", 145),
        ("Duration", 85),
        ("Size", 90),
        ("Views", 90),
    )

    def __init__(self) -> None:
        super().__init__()
        self._records: list[types.VideoRecord] = []

    def set_records(self, records: list[types.VideoRecord]) -> None:
        """Replace table contents in one reset for reliable proxy filtering.

        Example: `model.set_records(db.videos())` after a check.
        """

        self.beginResetModel()
        self._records = records
        self.endResetModel()

    def record(self, row: int) -> types.VideoRecord | None:
        """Return the catalog object behind a valid model row.

        Example: `record = model.record(source_index.row())`.
        """

        return self._records[row] if 0 <= row < len(self._records) else None

    def rowCount(self, parent: QtCore.QModelIndex = QtCore.QModelIndex()) -> int:
        """Return only top-level video rows.

        Example: Qt calls `rowCount()` while laying out the table.
        """

        return 0 if parent.isValid() else len(self._records)

    def columnCount(self, parent: QtCore.QModelIndex = QtCore.QModelIndex()) -> int:
        """Return the stable number of video fields shown by the table.

        Example: Qt calls `columnCount()` for the header.
        """

        return 0 if parent.isValid() else len(self._columns)

    def headerData(
        self,
        section: int,
        orientation: QtCore.Qt.Orientation,
        role: int = QtCore.Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        """Provide horizontal field names and readable default widths.

        Example: Qt requests `headerData(0, Horizontal)`.
        """

        if orientation == QtCore.Qt.Orientation.Horizontal and role == QtCore.Qt.ItemDataRole.DisplayRole:
            return self._columns[section][0]
        if orientation == QtCore.Qt.Orientation.Horizontal and role == QtCore.Qt.ItemDataRole.SizeHintRole:
            return QtCore.QSize(self._columns[section][1], 34)
        return None

    def data(self, index: QtCore.QModelIndex, role: int = QtCore.Qt.ItemDataRole.DisplayRole) -> Any:
        """Render, sort, color, and expose one nested video record field.

        Example: Qt requests `data(index, DisplayRole)` for a cell.
        """

        if not index.isValid():
            return None
        record = self._records[index.row()]
        display, sort_value = self._cell_values(record, index.column())
        if role == QtCore.Qt.ItemDataRole.DisplayRole:
            return display
        if role == SORT_ROLE:
            return sort_value
        if role == QtCore.Qt.ItemDataRole.UserRole:
            return record
        if role == QtCore.Qt.ItemDataRole.ToolTipRole:
            return record.download_error or record.meta.identity.title
        if role == QtCore.Qt.ItemDataRole.ForegroundRole and index.column() == 0:
            colors = {
                "Downloaded": "#58d68d",
                "Error": "#ff7675",
                "Live now": "#ff6b6b",
                "Upcoming": "#aeb6bf",
                "Available": "#74b9ff",
            }
            return QtGui.QBrush(QtGui.QColor(colors[display]))
        if role == QtCore.Qt.ItemDataRole.TextAlignmentRole and index.column() in {5, 6, 7}:
            return int(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
        return None

    @staticmethod
    def _cell_values(record: types.VideoRecord, column: int) -> tuple[str, object]:
        """Keep display and sort values co-located for each visible field.

        Example: `_cell_values(record, 1)` yields the title twice.
        """

        meta = record.meta
        local = record.local
        values = (
            (video_status(record), video_status(record)),
            (meta.identity.title, meta.identity.title.casefold()),
            (meta.origin.channel, meta.origin.channel.casefold()),
            (format_timestamp(meta.origin.published_at), meta.origin.published_at or 0),
            (format_timestamp(local.downloaded_at if local else None), local.downloaded_at if local else 0),
            (format_duration(meta.details.duration), meta.details.duration or 0),
            (format_size(local.size_bytes if local else None), local.size_bytes if local else 0),
            (
                f"{meta.details.view_count:,}" if meta.details.view_count is not None else "—",
                meta.details.view_count or 0,
            ),
        )
        return values[column]


class VideoFilterModel(QtCore.QSortFilterProxyModel):
    """Apply instant title/channel/ID search while preserving table sorting.

    Example: `proxy.set_search("lecture")` filters across useful text.
    """

    def __init__(self) -> None:
        super().__init__()
        self._search = ""
        self.setSortRole(SORT_ROLE)
        self.setDynamicSortFilter(True)

    def set_search(self, text: str) -> None:
        """Update the case-insensitive free-text table filter.

        Example: `proxy.set_search(search_box.text())`.
        """

        self._search = text.strip().casefold()
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QtCore.QModelIndex) -> bool:
        """Match search text against title, channel, and YouTube ID.

        Example: Qt calls this for each candidate source row.
        """

        if not self._search:
            return True
        model = self.sourceModel()
        if not isinstance(model, VideoTableModel):
            return True
        record = model.record(source_row)
        if not record:
            return False
        ident = record.meta.identity
        haystack = " ".join((ident.title, record.meta.origin.channel, ident.video_id)).casefold()
        return self._search in haystack
