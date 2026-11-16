"""Polished native Qt window for browsing and automating the video library.

Example: `LibraryWindow(service).show()` starts the desktop companion.
"""

from __future__ import annotations

from datetime import datetime
from typing import NamedTuple

from PySide6 import QtCore
from PySide6 import QtGui
from PySide6 import QtWidgets

import yt_whisper_subs
from yt_whisper_subs import i18n
from yt_whisper_subs import library_chapter_actions
from yt_whisper_subs import library_channels
from yt_whisper_subs import library_layout
from yt_whisper_subs import library_metadata_db
from yt_whisper_subs import library_model
from yt_whisper_subs import library_notifications
from yt_whisper_subs import library_progress
from yt_whisper_subs import library_removal_actions
from yt_whisper_subs import library_service
from yt_whisper_subs import library_theme
from yt_whisper_subs import library_types as types
from yt_whisper_subs import library_views as views
from yt_whisper_subs import library_video_queue
from yt_whisper_subs import library_window_support
from yt_whisper_subs import library_window_actions
from yt_whisper_subs import library_widgets
from yt_whisper_subs import library_workers
from yt_whisper_subs import library_vocabulary
from yt_whisper_subs import pipeline_progress as progress


WINDOW_TITLE = i18n.mark("YouTube Library · yt-whisper-subs v{version}")


def _metadata_status_text(state: library_metadata_db.MetadataQueueState) -> str:
    """Render ready and cooled-down metadata as distinct footer states.

    Example: a failed sole lookup reads `1 deferred`, never `1 queued`.
    """

    if not state.pending:
        return i18n.tr('Metadata lookup queue: empty')
    if state.ready:
        deferred = i18n.tr(' · {deferred:,} deferred', deferred=state.deferred) if state.deferred else ""
        return i18n.tr('Metadata: {ready:,} queued{deferred} · ≤1/min', ready=state.ready, deferred=deferred)
    assert state.retry_at is not None
    retry = i18n.date_text(datetime.fromtimestamp(state.retry_at).astimezone(), '%a %H:%M')
    return i18n.tr('Metadata: {deferred:,} deferred · retry {retry}', deferred=state.deferred, retry=retry)


class HeaderUi(NamedTuple):
    """Group search and action controls owned by the top toolbar.

    Example: `ui.header.download.setEnabled(True)`.
    """

    search: QtWidgets.QLineEdit
    check: QtWidgets.QPushButton
    download: QtWidgets.QPushButton
    play: QtWidgets.QPushButton
    watched: QtWidgets.QToolButton
    remove: QtWidgets.QToolButton
    pause: QtWidgets.QPushButton
    cancel: QtWidgets.QPushButton


class CatalogUi(NamedTuple):
    """Group sidebar, table models, and selected-video detail state.

    Example: `ui.catalog.model.set_records(records)`.
    """

    channels: library_channels.ChannelList
    table: QtWidgets.QTableView
    model: library_model.VideoTableModel
    proxy: library_model.VideoFilterModel
    filters: library_widgets.SmartFilterBar
    splitter: QtWidgets.QSplitter
    detail: library_widgets.DetailPanel


class LibraryUi(NamedTuple):
    """Compose all durable widget references into one window attribute.

    Example: `ui.header.search.clear()` resets free-text filtering.
    """

    header: HeaderUi
    catalog: CatalogUi
    metadata_status: QtWidgets.QLabel
    queue_status: QtWidgets.QLabel
    check_status: QtWidgets.QLabel
    trace: library_widgets.ActivityTrace


class LibraryWindow(
    library_removal_actions.RemovalActionsMixin,
    library_window_actions.WindowActionsMixin,
    library_chapter_actions.ChapterActionsMixin,
    library_layout.LayoutMixin,
    library_video_queue.VideoQueueMixin,
    library_window_support.WindowRuntimeMixin,
    QtWidgets.QMainWindow,
):
    """Coordinate native widgets, service tasks, scheduling, and tray life.

    Example: `window = LibraryWindow(service); window.show()`.
    """

    practice_changed = QtCore.Signal()

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
        self._media_watcher = QtCore.QFileSystemWatcher(self)
        self._watch_local_paths()
        self._media_timer = QtCore.QTimer(self)
        self._media_timer.setSingleShot(True)
        self._media_timer.timeout.connect(self._scan_changed_media)
        self._media_watcher.directoryChanged.connect(self._local_files_changed)
        self._local_scan_needed = True
        self._busy = False
        self._active_progress: progress.Update | None = None
        self._metadata_active = False
        self._media_active = False
        self._media_task: library_workers.BackgroundTask | None = None
        self._filter_key: tuple[str, int | None] = ("all", None)
        self._channels_by_id: dict[int, types.Channel] = {}
        self._active_task: library_workers.BackgroundTask | None = None
        self._active_video_id: str | None = None
        self._video_queue: dict[str, library_video_queue.VideoWork] = {}
        self._play_after_pipeline: set[str] = set()
        self._paused_progress: progress.Update | None = None
        self._metadata_task: library_workers.BackgroundTask | None = None
        self._channel_tasks: dict[int, library_workers.BackgroundTask] = {}
        self._playback_tasks: dict[int, library_workers.BackgroundTask] = {}
        self._quitting = False
        self._vocabulary_dialog: library_vocabulary.VocabularyDialog | None = None
        self._practice_dialog: QtWidgets.QDialog | None = None
        self._ui = self._build_ui()
        self._layout_timer = QtCore.QTimer(self)
        self._layout_timer.setSingleShot(True)
        self._layout_timer.timeout.connect(self._store_ui_layout)
        self._restore_table_layout()
        self._restore_catalog_layout()
        saved_view = views.VideoView.from_key(
            self._service.db.setting("video_view", views.VideoView.ALL.key)
        )
        self._ui.catalog.proxy.set_view(saved_view)
        self._ui.catalog.filters.set_view(saved_view)
        self._ui.trace.append_message(i18n.tr('Library started · output root: {out_dir}', out_dir=self._service.out_dir))
        self._tray = self._build_tray()
        self._notifications = library_notifications.TrayNotifications(
            service.db, self._tray, practice_window=lambda: self._practice_dialog,
        )
        self._notifications.video_requested.connect(self._notification_video)
        self._notifications.settings_requested.connect(self._notification_settings)
        self._notifications.practice.requested.connect(self._show_practice)
        self._notifications.practice.settings_requested.connect(self._notification_settings)
        QtWidgets.QApplication.instance().aboutToQuit.connect(self._notifications.stop)
        self._connect_actions()
        self._build_menus()
        self.refresh()
        self._restore_interrupted_pipelines()
        self._schedule_next(initial=True)
        self._schedule_metadata_backfill()
        self._media_timer.start(0)
        self.destroyed.connect(self._service.vocabulary.close)
        self._service.vocabulary.resume()

    def _watch_local_paths(self) -> None:
        """Watch a few output directories for local edits without disk polling.

        Example: a newly created chapters directory joins the watcher.
        """

        watched = set(self._media_watcher.directories())
        paths = (
            self._service.out_dir,
            self._service.video_dir,
            self._service.metadata_dir,
            self._service.out_dir / "chapters",
        )
        for path in paths:
            if path.is_dir() and str(path) not in watched:
                self._media_watcher.addPath(str(path))

    def _local_files_changed(self, path: str) -> None:
        """Coalesce local edits, including creation of a formerly absent folder.

        Example: an external subtitle repair schedules one idle catalog scan.
        """

        self._watch_local_paths()
        self._local_scan_needed |= path != str(self._service.out_dir / "chapters")
        self._media_timer.start(3000)

    def _build_ui(self) -> LibraryUi:
        """Construct the cohesive sidebar, filters, table, and details layout.

        Example: called once by `LibraryWindow.__init__()`.
        """

        self.setWindowTitle(i18n.tr(WINDOW_TITLE, version=yt_whisper_subs.__version__))
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
        content_layout.addWidget(catalog.splitter, 1)
        root.addWidget(content, 1)
        self.setCentralWidget(central)

        trace = library_widgets.ActivityTrace(self)
        self.addDockWidget(QtCore.Qt.DockWidgetArea.BottomDockWidgetArea, trace)
        trace_visible = self._service.db.setting("trace_visible", "0") in {"1", "True", "true"}
        trace.setVisible(trace_visible)
        trace.visibilityChanged.connect(self._store_trace_visibility)

        metadata_status = QtWidgets.QLabel()
        queue_status = QtWidgets.QLabel()
        check_status = QtWidgets.QLabel()
        check_status.setObjectName("statLabel")
        self.statusBar().addPermanentWidget(queue_status)
        self.statusBar().addPermanentWidget(metadata_status)
        self.statusBar().addPermanentWidget(check_status)
        self.statusBar().showMessage(i18n.tr('Ready'))
        return LibraryUi(header, catalog, metadata_status, queue_status, check_status, trace)

    def _build_sidebar(self) -> tuple[QtWidgets.QFrame, QtWidgets.QListWidget]:
        """Create the library filters and channel-subscription list.

        Example: `_build_sidebar()` returns its frame and channel list.
        """

        sidebar = QtWidgets.QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(245)
        layout = QtWidgets.QVBoxLayout(sidebar)
        layout.setContentsMargins(16, 20, 16, 16)
        title = QtWidgets.QLabel(i18n.tr('YouTube Library'))
        title.setWordWrap(True)
        title.setObjectName("appTitle")
        subtitle = QtWidgets.QLabel(i18n.tr('Watch · track · subtitle'))
        subtitle.setWordWrap(True)
        subtitle.setObjectName("appSubtitle")
        channels = library_channels.ChannelList()
        channels.setObjectName("channelList")
        add = QtWidgets.QPushButton(i18n.tr('＋  Track channel'))
        add.setObjectName("primaryButton")
        add.clicked.connect(self._add_channel)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addSpacing(14)
        layout.addWidget(channels, 1)
        layout.addWidget(add)
        return sidebar, channels

    def _build_header(self) -> tuple[HeaderUi, QtWidgets.QHBoxLayout]:
        """Keep search and common video actions directly accessible.

        Example: `_build_header()` returns controls plus their layout.
        """

        layout = QtWidgets.QHBoxLayout()
        layout.setSpacing(6)
        search = QtWidgets.QLineEdit()
        search.setPlaceholderText(i18n.tr('Search title, channel, or YouTube ID…'))
        search.setClearButtonEnabled(True)
        search.setMinimumWidth(100)
        check = QtWidgets.QPushButton(i18n.tr('↻  Check now'))
        download = QtWidgets.QPushButton(i18n.tr('↓  Download'))
        download.setToolTip(i18n.tr('Download, repair, or queue the selected video'))
        play = QtWidgets.QPushButton(i18n.tr('▶  Play'))
        watched = QtWidgets.QToolButton()
        watched.setObjectName("watchedButton")
        watched.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonIconOnly)
        remove = QtWidgets.QToolButton()
        remove.setObjectName("dangerButton")
        remove.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextOnly)
        pause = QtWidgets.QPushButton(i18n.tr('Ⅱ  Pause'))
        pause.hide()
        cancel = QtWidgets.QPushButton(i18n.tr('■  Cancel'))
        cancel.setObjectName("dangerButton")
        cancel.hide()
        play.setObjectName("primaryButton")
        layout.addWidget(search, 1)
        layout.addWidget(check)
        layout.addWidget(download)
        layout.addWidget(pause)
        layout.addWidget(cancel)
        layout.addWidget(play)
        layout.addWidget(watched)
        layout.addWidget(remove)
        return HeaderUi(search, check, download, play, watched, remove, pause, cancel), layout

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
        table.setWordWrap(False)
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
        detail = library_widgets.DetailPanel()
        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        splitter.setObjectName("catalogSplitter")
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(9)
        splitter.addWidget(table)
        splitter.addWidget(detail)
        self._apply_default_catalog_layout(splitter)
        return CatalogUi(channels, table, model, proxy, filters, splitter, detail)

    def _connect_actions(self) -> None:
        """Wire model selections and controls after all widgets exist.

        Example: called once after `_build_ui()`.
        """

        self._ui.header.search.textChanged.connect(self._ui.catalog.proxy.set_search)
        self._ui.catalog.filters.view_changed.connect(self._video_view_changed)
        self._ui.catalog.filters.reset_requested.connect(self._clear_filters)
        self._ui.catalog.proxy.criteria_changed.connect(self._refresh_smart_filters)
        self._ui.catalog.model.facets_changed.connect(self._refresh_smart_filters)
        self._ui.header.check.clicked.connect(self.check_now)
        self._ui.header.download.clicked.connect(self._download_selected)
        self._ui.header.pause.clicked.connect(self._pause_active_task)
        self._ui.header.cancel.clicked.connect(self._cancel_active_task)
        self._ui.header.play.clicked.connect(self._play_selected)
        self._ui.catalog.detail.generate_requested.connect(self._generate_chapters_selected)
        self._ui.catalog.detail.chapter_activated.connect(self._play_chapter)
        self._ui.catalog.channels.currentItemChanged.connect(self._channel_filter_changed)
        self._ui.catalog.channels.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self._ui.catalog.channels.customContextMenuRequested.connect(self._show_channel_context_menu)
        selection = self._ui.catalog.table.selectionModel()
        selection.selectionChanged.connect(self._selection_changed)
        self._ui.catalog.table.doubleClicked.connect(self._activate_video)
        self._ui.catalog.table.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self._ui.catalog.table.customContextMenuRequested.connect(self._show_video_context_menu)
        header = self._ui.catalog.table.horizontalHeader()
        header.sectionMoved.connect(self._schedule_layout_store)
        header.sectionResized.connect(self._schedule_layout_store)
        self._ui.catalog.splitter.splitterMoved.connect(self._schedule_layout_store)
        app = QtWidgets.QApplication.instance()
        if app:
            app.aboutToQuit.connect(self._store_ui_layout)

    def _show_vocabulary(self) -> None:
        """Reuse one native vocabulary window; e.g. inspect repeated words during playback."""

        if self._vocabulary_dialog is None:
            self._vocabulary_dialog = library_vocabulary.VocabularyDialog(self._service, self)
            self._vocabulary_dialog.play_requested.connect(self._play_word)
            self._vocabulary_dialog.practice_requested.connect(self._show_practice)
            self._vocabulary_dialog.destroyed.connect(lambda: setattr(self, "_vocabulary_dialog", None))
        self._vocabulary_dialog.show()
        self._vocabulary_dialog.raise_()
        self._vocabulary_dialog.activateWindow()

    def _show_practice(self, activity: bool = False) -> None:
        """Reuse one adaptive practice window; e.g. open it from Vocabulary or the Library menu."""

        if self._notifications.practice.visible:
            self._notifications.practice.snooze()
        # Construct practice widgets on demand; background preparation shares the same durable catalog.
        from yt_whisper_subs import library_practice

        if self._practice_dialog is None:
            self._practice_dialog = library_practice.PracticeDialog(self._service, self)
            self._practice_dialog.destroyed.connect(lambda: setattr(self, "_practice_dialog", None))
            self._practice_dialog.activity_changed.connect(self._notifications.practice.check)
            self._practice_dialog.activity_changed.connect(self.practice_changed)
        self._practice_dialog.show_page(activity)
        self._practice_dialog.show()
        self._practice_dialog.raise_()
        self._practice_dialog.activateWindow()

    def _play_word(self, event: dict) -> None:
        """Replay the captured event independently of table selection; e.g. from another video."""

        def play(report) -> None:
            """Bind the immutable event into the existing playback lane; e.g. seek its cue."""

            self._service.play_occurrence(event, report)

        self._run_playback(i18n.tr('Opening saved word in mpv…'), play, lambda _: self.refresh())

    def _store_trace_visibility(self, visible: bool) -> None:
        """Remember whether the optional activity trace was left open.

        Example: closing the dock persists `trace_visible=0`.
        """

        self._service.db.set_setting("trace_visible", int(visible))

    @QtCore.Slot(int)
    def _video_view_changed(self, view_id: int) -> None:
        """Apply and remember one smart view selected from the filter bar.

        Example: clicking Continue filters to started, unfinished videos.
        """

        view = views.VideoView(view_id)
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
        self._video_view_changed(int(views.VideoView.ALL))
        self._refresh_smart_filters()

    @QtCore.Slot()
    def _focus_search(self) -> None:
        """Focus and select the search query through the standard Ctrl+F action.

        Example: View → Focus search makes keyboard filtering immediate.
        """

        self._ui.header.search.setFocus()
        self._ui.header.search.selectAll()

    @QtCore.Slot()
    def refresh(self) -> None:
        """Reload channels, filtered videos, counts, and selection actions.

        Example: `refresh()` follows every completed background mutation.
        """

        selected_video = self._selected_record()
        selected_id = selected_video.meta.identity.video_id if selected_video else None
        with self._preserve_catalog_scroll():
            self._refresh_channels()
            catalog = self._service.catalog()
            self._ui.catalog.model.set_records(
                catalog.records, catalog.issues, watch_policy=self._service.db.watch_policy(),
            )
            for job in self._service.interrupted_pipeline_jobs():
                label = i18n.tr('Interrupted · {label} · click Resume', label=job.label)
                interrupted = progress.make(job.video_id, progress.Stage.INTERRUPTED, job.fraction, label)
                self._ui.catalog.model.set_progress(interrupted)
            state = self._service.metadata_queue_state()
            self._ui.metadata_status.setText(_metadata_status_text(state))
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
            (i18n.tr('▦  All videos'), ("all", None)),
        ]
        for text, key in items:
            item = QtWidgets.QListWidgetItem(text)
            item.setData(QtCore.Qt.ItemDataRole.UserRole, key)
            widget.addItem(item)
        tracked = self._service.db.channels()
        self._channels_by_id = {channel.channel_id: channel for channel in tracked}
        pinned = [channel for channel in tracked if channel.pinned]
        unpinned = [channel for channel in tracked if not channel.pinned]
        sections = (("pinned", i18n.tr('★  PINNED'), pinned), ("unpinned", i18n.tr('OTHER CHANNELS'), unpinned))
        for kind, label, channels in sections:
            if not channels:
                continue
            heading = QtWidgets.QListWidgetItem(f"  {label} · {len(channels):,}")
            heading.setData(QtCore.Qt.ItemDataRole.UserRole, (kind, None))
            heading.setToolTip(i18n.tr('Show videos from pinned channels') if kind == 'pinned' else
                               i18n.tr('Show videos from other channels'))
            color = "#f1c75b" if kind == "pinned" else "#aeb8c6"
            heading.setForeground(QtGui.QColor(color))
            widget.addItem(heading)
            for channel in channels:
                widget.add_channel(channel)
        for row in range(widget.count()):
            item = widget.item(row)
            if item.data(QtCore.Qt.ItemDataRole.UserRole) == self._filter_key:
                widget.setCurrentItem(item)
                break
        if widget.currentItem() is None:
            widget.setCurrentRow(0)
            self._filter_key = ("all", None)
        widget.blockSignals(False)
        self._apply_channel_scope()

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
            QtWidgets.QMessageBox.warning(self, i18n.tr('Track channel'), str(exc))
            return
        self._filter_key = ("channel", channel.channel_id)
        self._refresh_channels()
        self._ui.catalog.table.clearSelection()
        self._ui.catalog.detail.set_record(None)
        self._update_actions()

        task = library_workers.ChannelCheckTask(self._service, channel.channel_id)
        self._queue_channel_task(i18n.tr('Adding {title}…', title=channel.title), task)

    def _remove_channel(self) -> None:
        """Confirm and remove the selected subscription and remote-only rows.

        Example: Channel → Stop tracking invokes `_remove_channel()`.
        """

        channel = self._selected_channel()
        if not channel:
            QtWidgets.QMessageBox.information(self, i18n.tr('Stop tracking'), i18n.tr('Select a tracked channel first.'))
            return
        answer = QtWidgets.QMessageBox.question(
            self,
            i18n.tr('Stop tracking channel?'),
            i18n.tr('Stop tracking {title}? Downloaded videos remain in the library.', title=channel.title),
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
            QtWidgets.QMessageBox.information(self, i18n.tr('Automatic download'), i18n.tr('Select a tracked channel first.'))
            return
        self._service.db.set_channel_auto_download(channel.channel_id, not channel.auto_download)
        self._refresh_channels()
        self._update_actions()
