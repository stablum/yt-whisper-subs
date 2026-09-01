"""Qt table model and formatting policy for the native video catalog.

Example: `VideoTableModel().set_records(db.videos())`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from typing import NamedTuple

from PySide6 import QtCore

from yt_whisper_subs import library_types as types
from yt_whisper_subs import playback_progress as playback
from yt_whisper_subs import pipeline_progress as progress


SORT_ROLE = QtCore.Qt.ItemDataRole.UserRole + 1
PROGRESS_ROLE = QtCore.Qt.ItemDataRole.UserRole + 2
WATCHED_ROLE = QtCore.Qt.ItemDataRole.UserRole + 3

PIPELINE_COLUMN = 0
WATCHED_COLUMN = 1
TITLE_COLUMN = 2
PUBLISHED_COLUMN = 4
DURATION_COLUMN = 6
SIZE_COLUMN = 7
VIEWS_COLUMN = 8


class WatchedProgress(NamedTuple):
    """Provide the watched bar's normalized value, wording, and explanation.

    Example: `WatchedProgress(.5, "50%", tooltip, False)` paints halfway.
    """

    fraction: float
    label: str
    tooltip: str
    completed: bool


def format_timestamp(value: int | None) -> str:
    """Render a local date and time without inventing unavailable metadata.

    Example: `format_timestamp(None)` returns an em dash.
    """

    if value is None:
        return "—"
    try:
        timestamp = datetime.fromtimestamp(value).astimezone()
    except (OSError, OverflowError, ValueError):
        return "—"
    return timestamp.strftime("%Y-%m-%d  %H:%M")


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


def watched_progress(
    record: types.VideoRecord,
    update: playback.Update | None = None,
) -> WatchedProgress | None:
    """Combine durable and live state while reserving 100 percent for EOF.

    Example: `watched_progress(record).fraction` feeds sorting and painting.
    """

    if not record.downloaded:
        return None
    stored = record.playback
    stored_position = stored.position_seconds if stored else 0.0
    live_position = update.position_seconds if update else 0.0
    position = max(stored_position, live_position)
    duration_candidates = (
        update.duration_seconds if update else None,
        stored.duration_seconds if stored else None,
        record.meta.details.duration,
    )
    duration = next((value for value in duration_candidates if value and value > 0), None)
    completed = bool(stored and stored.completed_at is not None) or bool(update and update.completed)
    normalized = playback.make(record.meta.identity.video_id, position, duration, completed)
    fraction = playback.fraction(normalized)
    if completed:
        label = "✓ 100%"
        tooltip = (
            f"Completed {format_timestamp(stored.completed_at)}"
            if stored and stored.completed_at is not None
            else "Completed during this playback"
        )
    elif duration:
        label = f"{fraction:.0%}"
        tooltip = f"Watched to {format_duration(position)} of {format_duration(duration)}"
    elif position:
        label = "Started"
        tooltip = f"Watched to {format_duration(position)}; duration unavailable"
    else:
        label = "0%"
        tooltip = "Not watched yet"
    return WatchedProgress(fraction, label, tooltip, completed)


def record_progress(record: types.VideoRecord) -> progress.Update:
    """Represent durable catalog state through the shared progress vocabulary.

    Example: `record_progress(downloaded).stage` is `Stage.READY`.
    """

    video_id = record.meta.identity.video_id
    if record.downloaded:
        return progress.make(video_id, progress.Stage.READY, 1.0)
    if record.download_error:
        return progress.make(video_id, progress.Stage.FAILED, 0.0, f"Failed · {record.download_error}")
    live_status = record.meta.details.live_status
    if live_status == "is_live":
        return progress.make(video_id, progress.Stage.LIVE)
    if live_status == "is_upcoming":
        return progress.make(video_id, progress.Stage.UPCOMING)
    return progress.make(video_id, progress.Stage.AVAILABLE)


class VideoTableModel(QtCore.QAbstractTableModel):
    """Expose typed catalog records as a sortable, read-only Qt table.

    Example: `model.record(index.row())` returns the selected video.
    """

    _columns = (
        ("Pipeline", 230),
        ("Watched", 120),
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
        self._progress: dict[str, progress.Update] = {}
        self._watched: dict[str, playback.Update] = {}

    def set_records(self, records: list[types.VideoRecord]) -> None:
        """Replace table contents in one reset for reliable proxy filtering.

        Example: `model.set_records(db.videos())` after a check.
        """

        self.beginResetModel()
        self._records = records
        video_ids = {record.meta.identity.video_id for record in records}
        self._watched = {
            video_id: update
            for video_id, update in self._watched.items()
            if video_id in video_ids
        }
        self.endResetModel()

    def record(self, row: int) -> types.VideoRecord | None:
        """Return the catalog object behind a valid model row.

        Example: `record = model.record(source_index.row())`.
        """

        return self._records[row] if 0 <= row < len(self._records) else None

    def set_progress(self, update: progress.Update) -> None:
        """Store an ephemeral pipeline update and repaint its visible row.

        Example: `model.set_progress(update)` advances one video's bar.
        """

        self._progress[update.video_id] = update
        for row, record in enumerate(self._records):
            if record.meta.identity.video_id != update.video_id:
                continue
            cell = self.index(row, 0)
            self.dataChanged.emit(cell, cell, [QtCore.Qt.ItemDataRole.DisplayRole, PROGRESS_ROLE, SORT_ROLE])
            break

    def set_watched_progress(self, update: playback.Update) -> None:
        """Store one live mpv observation and repaint its graphical cell.

        Example: `model.set_watched_progress(update)` advances the watched bar.
        """

        self._watched[update.video_id] = update
        for row, record in enumerate(self._records):
            if record.meta.identity.video_id != update.video_id:
                continue
            cell = self.index(row, WATCHED_COLUMN)
            roles = [QtCore.Qt.ItemDataRole.DisplayRole, WATCHED_ROLE, SORT_ROLE]
            self.dataChanged.emit(cell, cell, roles)
            break

    def progress_at(self, row: int) -> progress.Update | None:
        """Return live progress or the durable fallback for a table row.

        Example: `model.progress_at(0)` supplies the progress delegate.
        """

        record = self.record(row)
        if not record:
            return None
        video_id = record.meta.identity.video_id
        return self._progress.get(video_id) or record_progress(record)

    def watched_at(self, row: int) -> WatchedProgress | None:
        """Return merged durable and live progress for one downloaded row.

        Example: `model.watched_at(0)` supplies the watched-bar delegate.
        """

        record = self.record(row)
        if not record:
            return None
        update = self._watched.get(record.meta.identity.video_id)
        return watched_progress(record, update)

    def title_for(self, video_id: str) -> str:
        """Resolve a progress event's video title for global status wording.

        Example: `model.title_for(update.video_id)` labels the status bar.
        """

        for record in self._records:
            if record.meta.identity.video_id == video_id:
                return record.meta.identity.title
        return video_id

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
        if (
            orientation == QtCore.Qt.Orientation.Horizontal
            and section == PIPELINE_COLUMN
            and role == QtCore.Qt.ItemDataRole.ToolTipRole
        ):
            return "Preparing · Download · Audio · Speech-to-text · Translation · Final files"
        if (
            orientation == QtCore.Qt.Orientation.Horizontal
            and section == WATCHED_COLUMN
            and role == QtCore.Qt.ItemDataRole.ToolTipRole
        ):
            return "Furthest observed position; 100% requires mpv to reach end-of-file"
        return None

    def data(self, index: QtCore.QModelIndex, role: int = QtCore.Qt.ItemDataRole.DisplayRole) -> Any:
        """Render, sort, color, and expose one nested video record field.

        Example: Qt requests `data(index, DisplayRole)` for a cell.
        """

        if not index.isValid():
            return None
        record = self._records[index.row()]
        display, sort_value = self._cell_values(record, index.column())
        update = self.progress_at(index.row())
        watched = self.watched_at(index.row())
        if index.column() == PIPELINE_COLUMN and update:
            display = update.label
            sort_value = progress.overall_fraction(update)
        elif index.column() == WATCHED_COLUMN:
            display = watched.label if watched else "—"
            sort_value = watched.fraction if watched else -1.0
        if role == QtCore.Qt.ItemDataRole.DisplayRole:
            return display
        if role == SORT_ROLE:
            return sort_value
        if role == QtCore.Qt.ItemDataRole.UserRole:
            return record
        if role == PROGRESS_ROLE:
            return update
        if role == WATCHED_ROLE:
            return watched
        if role == QtCore.Qt.ItemDataRole.ToolTipRole:
            if index.column() == PIPELINE_COLUMN and update:
                overall = progress.overall_fraction(update)
                return f"{update.label} · overall {overall:.0%}"
            if index.column() == WATCHED_COLUMN and watched:
                return watched.tooltip
            return record.download_error or record.meta.identity.title
        if role == QtCore.Qt.ItemDataRole.TextAlignmentRole and index.column() in {
            DURATION_COLUMN,
            SIZE_COLUMN,
            VIEWS_COLUMN,
        }:
            return int(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
        return None

    @staticmethod
    def _cell_values(record: types.VideoRecord, column: int) -> tuple[str, object]:
        """Keep display and sort values co-located for each visible field.

        Example: `_cell_values(record, 1)` yields the title twice.
        """

        meta = record.meta
        local = record.local
        state = record_progress(record)
        values = (
            (state.label, progress.overall_fraction(state)),
            ("—", -1.0),
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
