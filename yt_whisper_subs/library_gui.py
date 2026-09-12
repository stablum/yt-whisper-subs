"""Polished native Qt window for browsing and automating the video library.

Example: `LibraryWindow(service).show()` starts the desktop companion.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import NamedTuple

from PySide6 import QtCore
from PySide6 import QtGui
from PySide6 import QtWidgets

from yt_whisper_subs import library_chapter_actions
from yt_whisper_subs import library_model
from yt_whisper_subs import library_progress
from yt_whisper_subs import library_service
from yt_whisper_subs import library_theme
from yt_whisper_subs import library_types as types
from yt_whisper_subs import library_window_support
from yt_whisper_subs import library_window_actions
from yt_whisper_subs import library_widgets
from yt_whisper_subs import library_workers
from yt_whisper_subs import pipeline_progress as progress


_TABLE_LAYOUT_SETTING = "video_table_header_v1"
_TABLE_LAYOUT_SAVE_DELAY_MS = 250


class HeaderUi(NamedTuple):
    """Group search and action controls owned by the top toolbar.

    Example: `ui.header.download.setEnabled(True)`.
    """

    search: QtWidgets.QLineEdit
    check: QtWidgets.QPushButton
    download: QtWidgets.QPushButton
    play: QtWidgets.QPushButton
    cancel: QtWidgets.QPushButton


class CatalogUi(NamedTuple):
    """Group sidebar, table models, and selected-video detail state.

    Example: `ui.catalog.model.set_records(records)`.
    """

    channels: QtWidgets.QListWidget
    table: QtWidgets.QTableView
    model: library_model.VideoTableModel
    proxy: library_model.VideoFilterModel
    filters: library_widgets.SmartFilterBar
    detail: library_widgets.DetailPanel


class LibraryUi(NamedTuple):
    """Compose all durable widget references into one window attribute.

    Example: `ui.header.search.clear()` resets free-text filtering.
    """

    header: HeaderUi
    catalog: CatalogUi
    metadata_status: QtWidgets.QLabel
    next_check: QtWidgets.QLabel
    trace: library_widgets.ActivityTrace


class LibraryWindow(
    library_window_actions.WindowActionsMixin,
    library_chapter_actions.ChapterActionsMixin,
    library_window_support.WindowRuntimeMixin,
    QtWidgets.QMainWindow,
):
    """Coordinate native widgets, service tasks, scheduling, and tray life.

    Example: `window = LibraryWindow(service); window.show()`.
    """

    def __init__(self, service: library_service.LibraryService) -> None:
        super().__init__()
        self._service = service
        self._pool = QtCore.QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._playback_pool = QtCore.QThreadPool(self)
        playback_threads = max(2, QtCore.QThread.idealThreadCount())
        self._playback_pool.setMaxThreadCount(playback_threads)
        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._scheduled_check)
        self._metadata_timer = QtCore.QTimer(self)
        self._metadata_timer.setSingleShot(True)
        self._metadata_timer.timeout.connect(self._metadata_tick)
        self._busy = False
        self._active_progress: progress.Update | None = None
        self._metadata_active = False
        self._filter_key: tuple[str, int | None] = ("all", None)
        self._active_task: library_workers.BackgroundTask | None = None
        self._metadata_task: library_workers.BackgroundTask | None = None
        self._channel_tasks: dict[int, library_workers.BackgroundTask] = {}
        self._playback_tasks: dict[int, library_workers.BackgroundTask] = {}
        self._quitting = False
        self._ui = self._build_ui()
        self._table_layout_timer = QtCore.QTimer(self)
        self._table_layout_timer.setSingleShot(True)
        self._table_layout_timer.timeout.connect(self._store_table_layout)
        self._restore_table_layout()
        saved_view = library_model.VideoView.from_key(
            self._service.db.setting("video_view", library_model.VideoView.ALL.key)
        )
        self._ui.catalog.proxy.set_view(saved_view)
        self._ui.catalog.filters.set_view(saved_view)
        self._ui.trace.append_message(f"Library started · output root: {self._service.out_dir}")
        self._tray = self._build_tray()
        self._connect_actions()
        self._build_menus()
        self.refresh()
        self._schedule_next(initial=True)
        self._schedule_metadata_backfill()

    def _build_ui(self) -> LibraryUi:
        """Construct the cohesive sidebar, filters, table, and details layout.

        Example: called once by `LibraryWindow.__init__()`.
        """

        self.setWindowTitle("YouTube Library · yt-whisper-subs")
        self.resize(1480, 900)
        self.setMinimumSize(1050, 650)
        self.setStyleSheet(library_theme.STYLE)
        central = QtWidgets.QWidget()
        root = QtWidgets.QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        sidebar, channels = self._build_sidebar()
        root.addWidget(sidebar, 0)

        content = QtWidgets.QWidget()
        content_layout = QtWidgets.QVBoxLayout(content)
        content_layout.setContentsMargins(22, 18, 22, 16)
        content_layout.setSpacing(14)
        header, header_layout = self._build_header()
        catalog = self._build_catalog(channels)
        content_layout.addLayout(header_layout)
        content_layout.addWidget(catalog.filters)
        content_layout.addWidget(catalog.table, 1)
        content_layout.addWidget(catalog.detail)
        root.addWidget(content, 1)
        self.setCentralWidget(central)

        trace = library_widgets.ActivityTrace(self)
        self.addDockWidget(QtCore.Qt.DockWidgetArea.BottomDockWidgetArea, trace)
        trace_visible = self._service.db.setting("trace_visible", "0") in {"1", "True", "true"}
        trace.setVisible(trace_visible)
        trace.visibilityChanged.connect(self._store_trace_visibility)

        metadata_status = QtWidgets.QLabel()
        next_check = QtWidgets.QLabel()
        self.statusBar().addPermanentWidget(metadata_status)
        self.statusBar().addPermanentWidget(next_check)
        self.statusBar().showMessage("Ready")
        return LibraryUi(header, catalog, metadata_status, next_check, trace)

    def _build_sidebar(self) -> tuple[QtWidgets.QFrame, QtWidgets.QListWidget]:
        """Create the library filters and channel-subscription list.

        Example: `_build_sidebar()` returns its frame and channel list.
        """

        sidebar = QtWidgets.QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(245)
        layout = QtWidgets.QVBoxLayout(sidebar)
        layout.setContentsMargins(16, 20, 16, 16)
        title = QtWidgets.QLabel("YouTube Library")
        title.setObjectName("appTitle")
        subtitle = QtWidgets.QLabel("Watch · track · subtitle")
        subtitle.setObjectName("appSubtitle")
        channels = QtWidgets.QListWidget()
        channels.setObjectName("channelList")
        add = QtWidgets.QPushButton("＋  Track channel")
        add.setObjectName("primaryButton")
        add.clicked.connect(self._add_channel)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addSpacing(14)
        layout.addWidget(channels, 1)
        layout.addWidget(add)
        return sidebar, channels

    def _build_header(self) -> tuple[HeaderUi, QtWidgets.QHBoxLayout]:
        """Create search, manual check, download, and playback actions.

        Example: `_build_header()` returns controls plus their layout.
        """

        layout = QtWidgets.QHBoxLayout()
        search = QtWidgets.QLineEdit()
        search.setPlaceholderText("Search title, channel, or YouTube ID…")
        search.setClearButtonEnabled(True)
        search.setMinimumWidth(360)
        check = QtWidgets.QPushButton("↻  Check now")
        download = QtWidgets.QPushButton("↓  Download")
        play = QtWidgets.QPushButton("▶  Play")
        cancel = QtWidgets.QPushButton("■  Cancel")
        cancel.setObjectName("dangerButton")
        cancel.hide()
        play.setObjectName("primaryButton")
        layout.addWidget(search, 1)
        layout.addWidget(check)
        layout.addWidget(download)
        layout.addWidget(cancel)
        layout.addWidget(play)
        return HeaderUi(search, check, download, play, cancel), layout

    def _build_catalog(self, channels: QtWidgets.QListWidget) -> CatalogUi:
        """Create the model-backed table and selected-video detail panel.

        Example: `_build_catalog(channels).table` is sortable by every column.
        """

        model = library_model.VideoTableModel()
        proxy = library_model.VideoFilterModel()
        proxy.setSourceModel(model)
        table = QtWidgets.QTableView()
        table.setModel(proxy)
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSortingEnabled(True)
        table.sortByColumn(library_model.PUBLISHED_COLUMN, QtCore.Qt.SortOrder.DescendingOrder)
        table.setItemDelegateForColumn(
            library_model.PIPELINE_COLUMN,
            library_progress.PipelineProgressDelegate(table),
        )
        table.setItemDelegateForColumn(
            library_model.WATCHED_COLUMN,
            library_progress.WatchedProgressDelegate(table),
        )
        table.verticalHeader().hide()
        table.verticalHeader().setDefaultSectionSize(48)
        table.setHorizontalScrollMode(QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._apply_default_table_layout(table, model)
        filters = library_widgets.SmartFilterBar()
        return CatalogUi(channels, table, model, proxy, filters, library_widgets.DetailPanel())

    @staticmethod
    def _apply_default_table_layout(
        table: QtWidgets.QTableView,
        model: library_model.VideoTableModel,
    ) -> None:
        """Enable interactive columns and apply the model's readable defaults.

        Example: View → Reset column layout invokes this for the video table.
        """

        header = table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionsMovable(True)
        header.setFirstSectionMovable(True)
        header.setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Interactive)
        for visual_idx, column in enumerate(range(model.columnCount())):
            header.moveSection(header.visualIndex(column), visual_idx)
            size = model.headerData(
                column,
                QtCore.Qt.Orientation.Horizontal,
                QtCore.Qt.ItemDataRole.SizeHintRole,
            )
            if isinstance(size, QtCore.QSize):
                table.setColumnWidth(column, size.width())

    def _connect_actions(self) -> None:
        """Wire model selections and controls after all widgets exist.

        Example: called once after `_build_ui()`.
        """

        self._ui.header.search.textChanged.connect(self._ui.catalog.proxy.set_search)
        self._ui.catalog.filters.view_changed.connect(self._video_view_changed)
        self._ui.catalog.filters.reset_requested.connect(self._clear_filters)
        self._ui.catalog.proxy.criteria_changed.connect(self._refresh_smart_filters)
        self._ui.catalog.model.dataChanged.connect(self._refresh_smart_filters)
        self._ui.header.check.clicked.connect(self.check_now)
        self._ui.header.download.clicked.connect(self._download_selected)
        self._ui.header.cancel.clicked.connect(self._cancel_active_task)
        self._ui.header.play.clicked.connect(self._play_selected)
        self._ui.catalog.detail.generate_requested.connect(self._generate_chapters_selected)
        self._ui.catalog.detail.chapter_activated.connect(self._play_chapter)
        self._ui.catalog.channels.currentItemChanged.connect(self._channel_filter_changed)
        selection = self._ui.catalog.table.selectionModel()
        selection.selectionChanged.connect(self._selection_changed)
        self._ui.catalog.table.doubleClicked.connect(self._activate_video)
        header = self._ui.catalog.table.horizontalHeader()
        header.sectionMoved.connect(self._schedule_table_layout_store)
        header.sectionResized.connect(self._schedule_table_layout_store)
        app = QtWidgets.QApplication.instance()
        if app:
            app.aboutToQuit.connect(self._store_table_layout)

    def _build_menus(self) -> None:
        """Expose less-frequent channel, settings, and quit actions natively.

        Example: called once after action wiring.
        """

        file_menu = self.menuBar().addMenu("Library")
        settings = file_menu.addAction("Settings…")
        settings.triggered.connect(self._edit_settings)
        file_menu.addSeparator()
        quit_action = file_menu.addAction("Quit")
        quit_action.triggered.connect(self._quit)
        channel_menu = self.menuBar().addMenu("Channel")
        channel_menu.addAction("Track channel…", self._add_channel)
        channel_menu.addAction("Toggle automatic download", self._toggle_channel_auto)
        channel_menu.addAction("Stop tracking", self._remove_channel)
        video_menu = self.menuBar().addMenu("Video")
        video_menu.addAction("Download or repair selected", self._download_selected)
        video_menu.addAction("Play selected", self._play_selected)
        video_menu.addAction("Generate chapters", self._generate_chapters_selected)
        video_menu.addAction("Open on YouTube", self._open_selected_url)
        video_menu.addSeparator()
        self._cancel_action = video_menu.addAction("Cancel current operation", self._cancel_active_task)
        self._cancel_action.setShortcut(QtGui.QKeySequence("Ctrl+Shift+X"))
        self._remove_action = video_menu.addAction("Remove download and yields…", self._remove_selected)
        self._remove_action.setShortcut(QtGui.QKeySequence("Shift+Delete"))
        view_menu = self.menuBar().addMenu("View")
        search_action = view_menu.addAction("Focus search")
        search_action.setShortcut(QtGui.QKeySequence.StandardKey.Find)
        search_action.triggered.connect(self._focus_search)
        clear_filters = view_menu.addAction("Clear filters")
        clear_filters.setShortcut(QtGui.QKeySequence("Ctrl+Shift+F"))
        clear_filters.triggered.connect(self._clear_filters)
        view_menu.addSeparator()
        view_menu.addAction("Reset column layout", self._reset_table_layout)
        view_menu.addSeparator()
        trace_action = self._ui.trace.toggleViewAction()
        trace_action.setText("Activity trace")
        trace_action.setShortcut(QtGui.QKeySequence("Ctrl+Shift+L"))
        view_menu.addAction(trace_action)

    def _store_trace_visibility(self, visible: bool) -> None:
        """Remember whether the optional activity trace was left open.

        Example: closing the dock persists `trace_visible=0`.
        """

        self._service.db.set_setting("trace_visible", int(visible))

    def _restore_table_layout(self) -> None:
        """Restore Qt's versioned header state after applying safe defaults.

        Example: reopening the library restores column widths and positions.
        """

        encoded = self._service.db.setting(_TABLE_LAYOUT_SETTING, "")
        if not encoded:
            return
        try:
            state = QtCore.QByteArray.fromBase64(encoded.encode("ascii"))
        except UnicodeEncodeError:
            return
        self._ui.catalog.table.horizontalHeader().restoreState(state)

    def _schedule_table_layout_store(self, *_change: int) -> None:
        """Debounce repeated drag updates into one small settings write.

        Example: resizing a column restarts the 250 ms save timer.
        """

        self._table_layout_timer.start(_TABLE_LAYOUT_SAVE_DELAY_MS)

    def _store_table_layout(self) -> None:
        """Persist the native header state containing widths and visual order.

        Example: application shutdown flushes the latest table arrangement.
        """

        header = self._ui.catalog.table.horizontalHeader()
        encoded = bytes(header.saveState().toBase64()).decode("ascii")
        self._service.db.set_setting(_TABLE_LAYOUT_SETTING, encoded)

    def _reset_table_layout(self) -> None:
        """Return every video column to its shipped order and width.

        Example: View → Reset column layout repairs an awkward arrangement.
        """

        catalog = self._ui.catalog
        self._apply_default_table_layout(catalog.table, catalog.model)
        self._table_layout_timer.stop()
        self._store_table_layout()

    @QtCore.Slot(int)
    def _video_view_changed(self, view_id: int) -> None:
        """Apply and remember one smart view selected from the filter bar.

        Example: clicking Continue filters to started, unfinished videos.
        """

        view = library_model.VideoView(view_id)
        self._ui.catalog.proxy.set_view(view)
        self._ui.catalog.filters.set_view(view)
        self._service.db.set_setting("video_view", view.key)

    @QtCore.Slot()
    def _refresh_smart_filters(self) -> None:
        """Update facet counts and result wording from the current proxy scope.

        Example: search and live mpv progress both refresh the chip counts.
        """

        proxy = self._ui.catalog.proxy
        self._ui.catalog.filters.set_counts(
            proxy.facet_counts(),
            proxy.rowCount(),
            bool(self._ui.header.search.text().strip()),
        )

    @QtCore.Slot()
    def _clear_filters(self) -> None:
        """Reset search and smart view while preserving channel selection.

        Example: Ctrl+Shift+F restores every video in the selected channel.
        """

        self._ui.header.search.clear()
        self._video_view_changed(int(library_model.VideoView.ALL))
        self._refresh_smart_filters()

    @QtCore.Slot()
    def _focus_search(self) -> None:
        """Focus and select the search query through the standard Ctrl+F action.

        Example: View → Focus search makes keyboard filtering immediate.
        """

        self._ui.header.search.setFocus()
        self._ui.header.search.selectAll()

    def refresh(self) -> None:
        """Reload channels, filtered videos, counts, and selection actions.

        Example: `refresh()` follows every completed background mutation.
        """

        selected_video = self._selected_record()
        selected_id = selected_video.meta.identity.video_id if selected_video else None
        self._refresh_channels()
        _, channel_id = self._filter_key
        records = self._service.db.videos(channel_id=channel_id)
        self._ui.catalog.model.set_records(records)
        metadata_count = self._service.db.metadata_backlog_count()
        metadata_text = (
            f"Metadata: {metadata_count:,} queued · ≤1/min"
            if metadata_count
            else "Metadata: complete"
        )
        self._ui.metadata_status.setText(metadata_text)
        self._refresh_smart_filters()
        self._restore_video_selection(selected_id)
        self._update_actions()

    def _refresh_channels(self) -> None:
        """Rebuild the sidebar while preserving the logical selected filter.

        Example: `_refresh_channels()` reflects a newly tracked channel.
        """

        widget = self._ui.catalog.channels
        widget.blockSignals(True)
        widget.clear()
        items: list[tuple[str, tuple[str, int | None]]] = [
            ("▦  All videos", ("all", None)),
        ]
        for text, key in items:
            item = QtWidgets.QListWidgetItem(text)
            item.setData(QtCore.Qt.ItemDataRole.UserRole, key)
            widget.addItem(item)
        tracked = self._service.db.channels()
        heading = QtWidgets.QListWidgetItem(f"  TRACKED CHANNELS · {len(tracked):,}")
        heading.setFlags(QtCore.Qt.ItemFlag.NoItemFlags)
        heading.setForeground(QtGui.QColor("#778292"))
        widget.addItem(heading)
        for channel in tracked:
            auto = "⚡" if channel.auto_download else "  "
            error = "  !" if channel.last_error else ""
            item = QtWidgets.QListWidgetItem(f"{auto}  {channel.title}{error}")
            item.setData(QtCore.Qt.ItemDataRole.UserRole, ("channel", channel.channel_id))
            item.setToolTip(channel.last_error or channel.url)
            widget.addItem(item)
        for row in range(widget.count()):
            item = widget.item(row)
            if item.data(QtCore.Qt.ItemDataRole.UserRole) == self._filter_key:
                widget.setCurrentItem(item)
                break
        if widget.currentItem() is None:
            widget.setCurrentRow(0)
            self._filter_key = ("all", None)
        widget.blockSignals(False)

    def _add_channel(self) -> None:
        """Show a subscription immediately, then queue its network hydration.

        Example: the sidebar button invokes `_add_channel()`.
        """

        dialog = library_widgets.AddChannelDialog(self)
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        value, auto = dialog.values()
        try:
            channel = self._service.track_channel(value, auto)
        except ValueError as exc:
            QtWidgets.QMessageBox.warning(self, "Track channel", str(exc))
            return
        self._filter_key = ("channel", channel.channel_id)
        self.refresh()

        def initialize(report: Callable[[str], None]) -> types.Channel:
            """Bind the persisted placeholder into its queued network check.

            Example: `initialize(report)` replaces `@ruis` with its YouTube title.
            """

            return self._service.initialize_channel(channel.channel_id, report)

        self._queue_channel_task(f"Adding {channel.title}…", initialize)

    def _remove_channel(self) -> None:
        """Confirm and remove the selected subscription and remote-only rows.

        Example: Channel → Stop tracking invokes `_remove_channel()`.
        """

        channel = self._selected_channel()
        if not channel:
            QtWidgets.QMessageBox.information(self, "Stop tracking", "Select a tracked channel first.")
            return
        answer = QtWidgets.QMessageBox.question(
            self,
            "Stop tracking channel?",
            f"Stop tracking {channel.title}? Downloaded videos remain in the library.",
        )
        if answer != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self._service.db.remove_channel(channel.channel_id)
        self._filter_key = ("all", None)
        self.refresh()

    def _toggle_channel_auto(self) -> None:
        """Toggle future-video auto-download for the selected channel.

        Example: Channel → Toggle automatic download invokes this method.
        """

        channel = self._selected_channel()
        if not channel:
            QtWidgets.QMessageBox.information(self, "Automatic download", "Select a tracked channel first.")
            return
        self._service.db.set_channel_auto_download(channel.channel_id, not channel.auto_download)
        self.refresh()
