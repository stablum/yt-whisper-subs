"""Own translated menu and tray actions separately from runtime scheduling.

Example: Settings is available in either workspace and the tray can open Practice directly.
"""

from PySide6 import QtCore
from PySide6 import QtGui
from PySide6 import QtWidgets

from yt_whisper_subs import i18n


class MenusMixin:
    """Keep action construction cohesive; e.g. shortcuts and localized captions are defined beside their menu entries."""

    def _build_menus(self) -> None:
        """Expose less-frequent channel, settings, and quit actions natively.

        Example: called once after action wiring.
        """

        file_menu = self.menuBar().addMenu(i18n.tr('Library'))
        settings = file_menu.addAction(i18n.tr('Settings…'))
        settings.triggered.connect(self._edit_settings)
        vocabulary = file_menu.addAction(i18n.tr('Vocabulary…'), self._show_vocabulary)
        vocabulary.setShortcut(QtGui.QKeySequence("Ctrl+Shift+W"))
        file_menu.addSeparator()
        quit_action = file_menu.addAction(i18n.tr('Quit'))
        quit_action.triggered.connect(self._quit)
        channel_menu = self.menuBar().addMenu(i18n.tr('Channel'))
        channel_menu.addAction(i18n.tr('Track channel…'), self._add_channel)
        self._pin_channel_action = channel_menu.addAction(i18n.tr('Pin selected channel'), self._toggle_channel_pin)
        self._pin_channel_action.setShortcut(QtGui.QKeySequence("Alt+P"))
        channel_menu.addAction(i18n.tr('Toggle automatic download'), self._toggle_channel_auto)
        channel_menu.addAction(i18n.tr('Stop tracking'), self._remove_channel)
        video_menu = self.menuBar().addMenu(i18n.tr('Video'))
        video_menu.addAction(i18n.tr('Download or repair selected'), self._download_selected)
        video_menu.addAction(i18n.tr('Play selected'), self._play_selected)
        video_menu.addAction(i18n.tr('Generate chapters'), self._generate_chapters_selected)
        video_menu.addAction(i18n.tr('Open on YouTube'), self._open_selected_url)
        self._watched_action = video_menu.addAction(i18n.tr('Mark as watched'), self._mark_watched)
        self._watched_action.setIcon(self.style().standardIcon(QtWidgets.QStyle.StandardPixmap.SP_DialogApplyButton))
        self._watched_action.setToolTip(i18n.tr('Mark the selected video watched without changing its playback percentage'))
        self._ui.header.watched.setDefaultAction(self._watched_action)
        video_menu.addSeparator()
        self._pause_action = video_menu.addAction(i18n.tr('Pause current pipeline'), self._pause_active_task)
        self._pause_action.setShortcut(QtGui.QKeySequence("Ctrl+Shift+P"))
        self._cancel_action = video_menu.addAction(i18n.tr('Cancel current operation'), self._cancel_active_task)
        self._cancel_action.setShortcut(QtGui.QKeySequence("Ctrl+Shift+X"))
        self._remove_action = video_menu.addAction(i18n.tr('Remove download and yields…'), self._remove_selected)
        self._remove_action.setShortcut(QtGui.QKeySequence("Shift+Delete"))
        self._remove_action.setIconText(i18n.tr('Remove…'))
        self._remove_action.setToolTip(
            i18n.tr('Remove the selected download and all generated files (Shift+Delete). Download and watch history stay in the catalog.')
        )
        # Let Qt share activation and availability with the menu and shortcut.
        self._ui.header.remove.setDefaultAction(self._remove_action)
        self._remove_filtered_action = video_menu.addAction(
            i18n.tr('Remove filtered downloads…'), self._remove_filtered,
        )
        self._remove_filtered_action.setToolTip(
            i18n.tr('Remove downloads and generated files for all current filter matches. Choose Watched to reclaim space while keeping watch history.')
        )
        self._ui.catalog.filters.set_removal_action(self._remove_filtered_action)
        view_menu = self.menuBar().addMenu(i18n.tr('View'))
        search_action = view_menu.addAction(i18n.tr('Focus search'))
        search_action.setShortcut(QtGui.QKeySequence.StandardKey.Find)
        search_action.triggered.connect(self._focus_search)
        clear_filters = view_menu.addAction(i18n.tr('Clear filters'))
        clear_filters.setShortcut(QtGui.QKeySequence("Ctrl+Shift+F"))
        clear_filters.triggered.connect(self._clear_filters)
        view_menu.addSeparator()
        view_menu.addAction(i18n.tr('Reset column layout'), self._reset_table_layout)
        view_menu.addAction(i18n.tr('Reset video inspector size'), self._reset_catalog_layout)
        view_menu.addSeparator()
        trace_action = self._ui.trace.toggleViewAction()
        trace_action.setText(i18n.tr('Activity trace'))
        trace_action.setShortcut(QtGui.QKeySequence("Ctrl+Shift+L"))
        view_menu.addAction(trace_action)
        # Alt+P plays dictation in Practice, and catalog edit keys belong to the catalog.
        catalog = self._workspaces.widget(0)
        for action in (self._pin_channel_action, self._remove_action, clear_filters):
            action.setShortcutContext(QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut)
            catalog.addAction(action)

    def _build_tray(self) -> QtWidgets.QSystemTrayIcon:
        """Create the system-tray lifetime needed for unattended checks.

        Example: closing the window leaves `_build_tray()` visible by default.
        """

        icon = self.style().standardIcon(QtWidgets.QStyle.StandardPixmap.SP_MediaPlay)
        self.setWindowIcon(icon)
        tray = QtWidgets.QSystemTrayIcon(icon, self)
        tray.setToolTip(i18n.tr('yt-whisper-subs YouTube Library'))
        menu = QtWidgets.QMenu(self)
        menu.addAction(i18n.tr('Show library'), self._show_library)
        menu.addAction(i18n.tr('Practice…'), self._show_practice)
        menu.addAction(i18n.tr('Practice activity…'), lambda: self._show_practice(True))
        menu.addAction(i18n.tr('Check now'), self.check_now)
        menu.addSeparator()
        menu.addAction(i18n.tr('Quit'), self._quit)
        self._tray_menu = menu
        tray.setContextMenu(menu)
        tray.activated.connect(self._tray_activated)
        if QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
            tray.show()
        return tray
