"""Qt table model and formatting policy for the native video catalog.

Example: `VideoTableModel().set_records(db.videos())`.
"""

from __future__ import annotations

from datetime import datetime
import enum
from typing import Any
from typing import NamedTuple

from PySide6 import QtCore

from yt_whisper_subs import library_artifacts
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
_UNCHECKED = object()


class WatchedProgress(NamedTuple):
    """Provide the watched bar's normalized value, wording, and explanation.

    Example: `WatchedProgress(.5, "50%", tooltip, False, True)` paints halfway.
    """

    fraction: float
    label: str
    tooltip: str
    completed: bool
    started: bool


class VideoView(enum.IntEnum):
    """Name each mutually exclusive task-oriented catalog view.

    Example: `VideoView.CONTINUE` selects videos started but not completed.
    """

    ALL = 0
    ON_DEVICE = 1
    AVAILABLE = 2
    UNWATCHED = 3
    CONTINUE = 4
    WATCHED = 5
    ISSUES = 6

    @property
    def key(self) -> str:
        """Return the stable lowercase value persisted in library settings.

        Example: `VideoView.ON_DEVICE.key` is `"on_device"`.
        """

        return self.name.lower()

    @classmethod
    def from_key(cls, key: str) -> VideoView:
        """Restore a persisted key, falling back safely to the full catalog.

        Example: `VideoView.from_key("watched")` restores that smart view.
        """

        try:
            return cls[key.upper()]
        except KeyError:
            return cls.ALL


class VideoViewSpec(NamedTuple):
    """Keep each smart view's wording and explanation beside its identity.

    Example: filter chips are generated directly from `VIDEO_VIEWS`.
    """

    view: VideoView
    label: str
    tooltip: str


VIDEO_VIEWS = (
    VideoViewSpec(VideoView.ALL, "All", "Every video in the current library or channel"),
    VideoViewSpec(VideoView.ON_DEVICE, "On device", "Downloaded videos ready to play"),
    VideoViewSpec(VideoView.AVAILABLE, "Available", "Tracked videos not downloaded yet"),
    VideoViewSpec(VideoView.UNWATCHED, "Unwatched", "Downloaded videos not started yet"),
    VideoViewSpec(VideoView.CONTINUE, "Continue", "Started videos that have not reached the end"),
    VideoViewSpec(VideoView.WATCHED, "Watched", "Videos confirmed complete by mpv"),
    VideoViewSpec(VideoView.ISSUES, "Issues", "Videos whose latest download or processing attempt failed"),
)


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
    return WatchedProgress(fraction, label, tooltip, completed, position > 0 or completed)


def accepts_view(
    record: types.VideoRecord,
    watched: WatchedProgress | None,
    view: VideoView,
    issue: str | None | object = _UNCHECKED,
) -> bool:
    """Apply one smart-view strategy without coupling it to Qt widgets.

    Example: `accepts_view(record, watched, VideoView.CONTINUE)` detects partial viewing.
    """

    if view is VideoView.ALL:
        return True
    if view is VideoView.ON_DEVICE:
        return record.downloaded
    if view is VideoView.AVAILABLE:
        return not record.downloaded
    if view is VideoView.ISSUES:
        resolved = library_artifacts.pipeline_issue(record) if issue is _UNCHECKED else issue
        return bool(record.download_error or resolved)
    if not watched:
        return False
    if view is VideoView.WATCHED:
        return watched.completed
    if view is VideoView.CONTINUE:
        return watched.started and not watched.completed
    if view is VideoView.UNWATCHED:
        return not watched.started
    return True


def record_progress(
    record: types.VideoRecord,
    issue: str | None | object = _UNCHECKED,
) -> progress.Update:
    """Represent durable catalog state through the shared progress vocabulary.

    Example: `record_progress(downloaded).stage` is `Stage.READY`.
    """

    video_id = record.meta.identity.video_id
    if record.download_error:
        return progress.make(video_id, progress.Stage.FAILED, 0.0, f"Failed · {record.download_error}")
    resolved = library_artifacts.pipeline_issue(record) if issue is _UNCHECKED else issue
    if resolved:
        return progress.make(video_id, progress.Stage.FAILED, 0.0, f"Incomplete · {resolved}")
    if record.downloaded:
        return progress.make(video_id, progress.Stage.READY, 1.0)
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

    facets_changed = QtCore.Signal()

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
        self._row_by_id: dict[str, int] = {}
        self._issues: dict[str, str | None] = {}
        self._durable_progress: dict[str, progress.Update] = {}
        self._progress: dict[str, progress.Update] = {}
        self._watched: dict[str, playback.Update] = {}
        self._watched_views: dict[str, WatchedProgress | None] = {}

    def set_records(
        self,
        records: list[types.VideoRecord],
        issues: dict[str, str | None] | None = None,
    ) -> None:
        """Replace table contents in one reset for reliable proxy filtering.

        Example: `model.set_records(db.videos())` after a check.
        """

        self.beginResetModel()
        self._records = records
        self._row_by_id = {
            record.meta.identity.video_id: row
            for row, record in enumerate(records)
        }
        self._issues = (
            issues
            if issues is not None
            else {
                record.meta.identity.video_id: library_artifacts.pipeline_issue(record)
                for record in records
            }
        )
        self._durable_progress = {
            record.meta.identity.video_id: record_progress(
                record,
                self._issues.get(record.meta.identity.video_id),
            )
            for record in records
        }
        video_ids = self._row_by_id.keys()
        self._progress = {
            video_id: update
            for video_id, update in self._progress.items()
            if video_id in video_ids
        }
        self._watched = {
            video_id: update
            for video_id, update in self._watched.items()
            if video_id in video_ids
        }
        self._watched_views = {
            record.meta.identity.video_id: watched_progress(
                record,
                self._watched.get(record.meta.identity.video_id),
            )
            for record in records
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
        row = self._row_by_id.get(update.video_id)
        if row is not None:
            cell = self.index(row, PIPELINE_COLUMN)
            roles = [QtCore.Qt.ItemDataRole.DisplayRole, PROGRESS_ROLE, SORT_ROLE]
            self.dataChanged.emit(cell, cell, roles)

    def set_watched_progress(self, update: playback.Update) -> None:
        """Store one live mpv observation and repaint its graphical cell.

        Example: `model.set_watched_progress(update)` advances the watched bar.
        """

        self._watched[update.video_id] = update
        row = self._row_by_id.get(update.video_id)
        if row is not None:
            self._watched_views[update.video_id] = watched_progress(self._records[row], update)
            cell = self.index(row, WATCHED_COLUMN)
            roles = [QtCore.Qt.ItemDataRole.DisplayRole, WATCHED_ROLE, SORT_ROLE]
            self.dataChanged.emit(cell, cell, roles)
            self.facets_changed.emit()

    def progress_at(self, row: int) -> progress.Update | None:
        """Return live progress or the durable fallback for a table row.

        Example: `model.progress_at(0)` supplies the progress delegate.
        """

        record = self.record(row)
        if not record:
            return None
        video_id = record.meta.identity.video_id
        return self._progress.get(video_id) or self._durable_progress.get(video_id)

    def issue_at(self, row: int) -> str | None:
        """Return cached artifact health without touching the filesystem.

        Example: smart views call `model.issue_at(row)` during filtering.
        """

        record = self.record(row)
        return self.issue_for(record.meta.identity.video_id) if record else None

    def issue_for(self, video_id: str) -> str | None:
        """Resolve cached health for action wording by stable YouTube ID.

        Example: `model.issue_for(video_id)` decides whether Repair is shown.
        """

        return self._issues.get(video_id)

    def set_issue(self, video_id: str, issue: str | None) -> None:
        """Reconcile one row after an explicit file-stamp health check.

        Example: selecting a row refreshes externally edited subtitle state.
        """

        if self._issues.get(video_id) == issue:
            return
        row = self._row_by_id.get(video_id)
        if row is None:
            return
        self._issues[video_id] = issue
        self._durable_progress[video_id] = record_progress(self._records[row], issue)
        cell = self.index(row, PIPELINE_COLUMN)
        roles = [QtCore.Qt.ItemDataRole.DisplayRole, PROGRESS_ROLE, SORT_ROLE]
        self.dataChanged.emit(cell, cell, roles)
        self.facets_changed.emit()

    def watched_at(self, row: int) -> WatchedProgress | None:
        """Return merged durable and live progress for one downloaded row.

        Example: `model.watched_at(0)` supplies the watched-bar delegate.
        """

        record = self.record(row)
        if not record:
            return None
        return self._watched_views.get(record.meta.identity.video_id)

    def title_for(self, video_id: str) -> str:
        """Resolve a progress event's video title for global status wording.

        Example: `model.title_for(update.video_id)` labels the status bar.
        """

        row = self._row_by_id.get(video_id)
        return self._records[row].meta.identity.title if row is not None else video_id

    def row_for(self, video_id: str) -> int | None:
        """Resolve a stable YouTube ID to its source row in constant time.

        Example: selection restoration calls `model.row_for(video_id)`.
        """

        return self._row_by_id.get(video_id)

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
        if role == QtCore.Qt.ItemDataRole.UserRole:
            return record
        if role == PROGRESS_ROLE:
            return self.progress_at(index.row())
        if role == WATCHED_ROLE:
            return self.watched_at(index.row())
        column = index.column()
        if role in {QtCore.Qt.ItemDataRole.DisplayRole, SORT_ROLE}:
            if column == PIPELINE_COLUMN:
                update = self.progress_at(index.row())
                if not update:
                    return None
                if role == QtCore.Qt.ItemDataRole.DisplayRole:
                    return update.label
                return progress.overall_fraction(update)
            if column == WATCHED_COLUMN:
                watched = self.watched_at(index.row())
                if role == QtCore.Qt.ItemDataRole.DisplayRole:
                    return watched.label if watched else "—"
                return watched.fraction if watched else -1.0
            display, sort_value = self._cell_values(record, column)
            return display if role == QtCore.Qt.ItemDataRole.DisplayRole else sort_value
        if role == QtCore.Qt.ItemDataRole.ToolTipRole:
            if column == PIPELINE_COLUMN and (update := self.progress_at(index.row())):
                overall = progress.overall_fraction(update)
                return f"{update.label} · overall {overall:.0%}"
            if column == WATCHED_COLUMN and (watched := self.watched_at(index.row())):
                return watched.tooltip
            return record.download_error or record.meta.identity.title
        if role == QtCore.Qt.ItemDataRole.TextAlignmentRole and column in {
            DURATION_COLUMN,
            SIZE_COLUMN,
            VIEWS_COLUMN,
        }:
            return int(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
        return None

    @staticmethod
    def _cell_values(record: types.VideoRecord, column: int) -> tuple[str, object]:
        """Keep display and sort values co-located for each visible field.

        Example: `_cell_values(record, 2)` yields display and sort title.
        """

        meta = record.meta
        if column == TITLE_COLUMN:
            return meta.identity.title, meta.identity.title.casefold()
        if column == 3:
            return meta.origin.channel, meta.origin.channel.casefold()
        if column == PUBLISHED_COLUMN:
            return format_timestamp(meta.origin.published_at), meta.origin.published_at or 0
        if column == 5:
            downloaded_at = record.local.downloaded_at if record.local else None
            return format_timestamp(downloaded_at), downloaded_at or 0
        if column == DURATION_COLUMN:
            return format_duration(meta.details.duration), meta.details.duration or 0
        if column == SIZE_COLUMN:
            size = record.local.size_bytes if record.local else None
            return format_size(size), size or 0
        if column == VIEWS_COLUMN:
            views = meta.details.view_count
            return f"{views:,}" if views is not None else "—", views or 0
        return "—", -1.0


class VideoFilterModel(QtCore.QSortFilterProxyModel):
    """Compose search and one task-oriented smart view with stable sorting.

    Example: `proxy.set_view(VideoView.CONTINUE)` shows partial viewing.
    """

    criteria_changed = QtCore.Signal()

    def __init__(self) -> None:
        super().__init__()
        self._search = ""
        self._view = VideoView.ALL
        self._channel_id: int | None = None
        self.setSortRole(SORT_ROLE)
        self.setDynamicSortFilter(True)

    def set_search(self, text: str) -> None:
        """Update the case-insensitive free-text table filter.

        Example: `proxy.set_search(search_box.text())`.
        """

        search = text.strip().casefold()
        if search == self._search:
            return
        self.beginFilterChange()
        self._search = search
        self.endFilterChange(QtCore.QSortFilterProxyModel.Direction.Rows)
        self.criteria_changed.emit()

    @property
    def view(self) -> VideoView:
        """Expose the active smart view for persistence and filter-bar state.

        Example: `proxy.view is VideoView.ALL` identifies an unfiltered catalog.
        """

        return self._view

    def set_view(self, view: VideoView) -> None:
        """Replace the active smart view while retaining search and sorting.

        Example: `proxy.set_view(VideoView.AVAILABLE)` shows remote-only rows.
        """

        if view is self._view:
            return
        self.beginFilterChange()
        self._view = view
        self.endFilterChange(QtCore.QSortFilterProxyModel.Direction.Rows)
        self.criteria_changed.emit()

    def set_channel(self, channel_id: int | None) -> None:
        """Filter the resident catalog by subscription without reloading SQL.

        Example: `proxy.set_channel(7)` switches the sidebar immediately.
        """

        if channel_id == self._channel_id:
            return
        self.beginFilterChange()
        self._channel_id = channel_id
        self.endFilterChange(QtCore.QSortFilterProxyModel.Direction.Rows)
        self.criteria_changed.emit()

    def facet_counts(self) -> dict[VideoView, int]:
        """Count every smart view within the current channel and search scope.

        Example: the filter bar uses `facet_counts()[VideoView.CONTINUE]`.
        """

        counts = {spec.view: 0 for spec in VIDEO_VIEWS}
        model = self.sourceModel()
        if not isinstance(model, VideoTableModel):
            return counts
        for row in range(model.rowCount()):
            record = model.record(row)
            if not record or not self._matches_scope(record) or not self._matches_search(record):
                continue
            watched = model.watched_at(row)
            issue = model.issue_at(row)
            counts[VideoView.ALL] += 1
            counts[VideoView.ON_DEVICE if record.downloaded else VideoView.AVAILABLE] += 1
            counts[VideoView.ISSUES] += int(bool(record.download_error or issue))
            if watched:
                if watched.completed:
                    counts[VideoView.WATCHED] += 1
                elif watched.started:
                    counts[VideoView.CONTINUE] += 1
                else:
                    counts[VideoView.UNWATCHED] += 1
        return counts

    def filterAcceptsRow(self, source_row: int, source_parent: QtCore.QModelIndex) -> bool:
        """Match search text against title, channel, and YouTube ID.

        Example: Qt calls this for each candidate source row.
        """

        model = self.sourceModel()
        if not isinstance(model, VideoTableModel):
            return True
        record = model.record(source_row)
        if not record:
            return False
        if not self._matches_scope(record) or not self._matches_search(record):
            return False
        if self._view is VideoView.ALL:
            return True
        watched = model.watched_at(source_row)
        issue = model.issue_at(source_row)
        return accepts_view(record, watched, self._view, issue)

    def _matches_scope(self, record: types.VideoRecord) -> bool:
        """Apply the selected sidebar channel against the in-memory record.

        Example: All videos uses a `None` channel and accepts every row.
        """

        return self._channel_id is None or record.subscription_id == self._channel_id

    def _matches_search(self, record: types.VideoRecord) -> bool:
        """Match one record against normalized title, channel, or YouTube ID text.

        Example: facet counts call this before evaluating every smart view.
        """

        if not self._search:
            return True
        ident = record.meta.identity
        haystack = " ".join((ident.title, record.meta.origin.channel, ident.video_id)).casefold()
        return self._search in haystack
