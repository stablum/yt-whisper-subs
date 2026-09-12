"""Selection, settings, download, and exact-removal actions for the library window.

Example: `LibraryWindow` mixes these slots into its native Qt shell.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6 import QtCore
from PySide6 import QtGui
from PySide6 import QtWidgets

from yt_whisper_subs import cfg
from yt_whisper_subs import library_model
from yt_whisper_subs import library_types as types
from yt_whisper_subs import library_widgets


class WindowActionsMixin:
    """Keep record selection and user-initiated mutations outside UI construction.

    Example: the Download button invokes `_download_selected()` from this mixin.
    """

    def _download_selected(self) -> None:
        """Download a remote row or repair an incomplete local pipeline.

        Example: a corrupt Dutch SRT changes the action from Play to Repair.
        """

        record = self._selected_record()
        if not record:
            return
        issue = library_model.local_pipeline_issue(record)
        if record.downloaded and not issue and not record.download_error:
            self._play_selected()
            return
        video_id = record.meta.identity.video_id

        def download(report: Callable[[str], None]) -> None:
            """Bind the selected video ID into a worker-safe call.

            Example: `download(report)` runs off the GUI thread.
            """

            self._service.download(video_id, report)

        label = "Repairing pipeline…" if record.downloaded else "Starting download…"
        self._run_task(label, download, lambda _: self.refresh())

    def _activate_video(self, index: QtCore.QModelIndex) -> None:
        """Play a complete row or immediately process a remote/incomplete row.

        Example: double-clicking Available starts without a confirmation prompt.
        """

        if not index.isValid():
            return
        self._ui.catalog.table.selectRow(index.row())
        record = self._record_from_proxy_index(index)
        issue = library_model.local_pipeline_issue(record) if record else None
        if record and record.downloaded and not issue and not record.download_error:
            self._play_selected()
        elif record:
            self._download_selected()

    def _remove_selected(self) -> None:
        """Confirm an exact manifest before removing one video's managed yields.

        Example: Video -> Remove lists every path and never uses a wildcard.
        """

        record = self._selected_record()
        if not record or not record.downloaded or self._busy:
            return
        video_id = record.meta.identity.video_id
        manifest = self._service.video_yields(video_id)
        if not manifest.paths:
            return
        box = QtWidgets.QMessageBox(
            QtWidgets.QMessageBox.Icon.Warning,
            "Remove download and yields?",
            f"Remove {record.meta.identity.title} and {len(manifest.paths)} managed file(s)?",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.Cancel,
            self,
        )
        box.setDefaultButton(QtWidgets.QMessageBox.StandardButton.Cancel)
        box.setInformativeText("The tracked catalog entry remains available for downloading again.")
        box.setDetailedText("\n".join(str(path) for path in manifest.paths))
        if box.exec() != QtWidgets.QMessageBox.StandardButton.Yes:
            return

        def remove(report: Callable[[str], None]) -> int:
            """Delete the inspected video's files one path at a time.

            Example: the serial worker reports every individual unlink operation.
            """

            return self._service.remove_yields(manifest, report)

        self._run_task("Removing download…", remove, lambda _: self.refresh())

    def _edit_settings(self) -> None:
        """Persist scheduling, retention, browser, and tray settings.

        Example: Library -> Settings invokes `_edit_settings()`.
        """

        raw_recent = self._service.db.setting(
            "channel_recent_limit",
            str(cfg.DEFAULT_LIBRARY_CHANNEL_RECENT_LIMIT),
        )
        try:
            recent = int(raw_recent)
        except ValueError:
            recent = cfg.DEFAULT_LIBRARY_CHANNEL_RECENT_LIMIT
        current = library_widgets.SettingsValues(
            float(self._service.db.setting("check_hours", str(cfg.DEFAULT_LIBRARY_CHECK_HOURS))),
            self._service.db.setting("cookies_from_browser", ""),
            self._service.db.setting(
                "minimize_to_tray",
                str(int(cfg.DEFAULT_LIBRARY_MINIMIZE_TO_TRAY)),
            )
            in {"1", "True", "true"},
            recent,
            self._service.db.setting("channel_published_after", ""),
        )
        dialog = library_widgets.SettingsDialog(current, self)
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        self._service.db.set_setting("check_hours", values.check_hours)
        self._service.db.set_setting("cookies_from_browser", values.cookies_from_browser)
        self._service.db.set_setting("minimize_to_tray", int(values.minimize_to_tray))
        self._service.db.set_setting("channel_recent_limit", values.channel_recent_limit)
        self._service.db.set_setting("channel_published_after", values.channel_published_after)
        self._service.reload_clients()
        pruned = self._service.apply_retention()
        if pruned:
            self._ui.trace.append_message(f"Retention · Removed {pruned:,} dated remote-only video(s)")
            self.refresh()
        self._schedule_next()

    def _open_selected_url(self) -> None:
        """Open the selected video's canonical YouTube page externally.

        Example: Video -> Open on YouTube invokes this method.
        """

        record = self._selected_record()
        if record:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl(record.meta.identity.url))

    def _channel_filter_changed(
        self,
        current: QtWidgets.QListWidgetItem | None,
        previous: QtWidgets.QListWidgetItem | None,
    ) -> None:
        """Reload table records for a sidebar library/channel filter.

        Example: Qt passes the new and previous sidebar items.
        """

        del previous
        if current and (key := current.data(QtCore.Qt.ItemDataRole.UserRole)):
            self._filter_key = tuple(key)
            self.refresh()

    def _selection_changed(self) -> None:
        """Update detail text and action availability for the selected row.

        Example: table selection changes invoke `_selection_changed()`.
        """

        record = self._selected_record()
        chapter_set = self._service.chapter_set(record.meta.identity.video_id) if record else None
        self._ui.catalog.detail.set_record(record, chapter_set)
        self._update_actions()

    def _selected_record(self) -> types.VideoRecord | None:
        """Resolve the current proxy selection back to its catalog record.

        Example: `_selected_record()` powers Download, Play, and Remove.
        """

        rows = self._ui.catalog.table.selectionModel().selectedRows()
        return self._record_from_proxy_index(rows[0]) if rows else None

    def _record_from_proxy_index(self, index: QtCore.QModelIndex) -> types.VideoRecord | None:
        """Map one proxy row to the typed source-model record.

        Example: `_record_from_proxy_index(table.currentIndex())`.
        """

        source = self._ui.catalog.proxy.mapToSource(index)
        return self._ui.catalog.model.record(source.row())

    def _selected_channel(self) -> types.Channel | None:
        """Return the sidebar channel object or None for library filters.

        Example: `_selected_channel()` gates channel menu actions.
        """

        kind, channel_id = self._filter_key
        return self._service.db.channel(channel_id) if kind == "channel" and channel_id else None

    def _restore_video_selection(self, video_id: str | None) -> None:
        """Restore a previous video selection after a model reset when possible.

        Example: `refresh()` calls `_restore_video_selection(id)`.
        """

        if not video_id:
            self._ui.catalog.detail.set_record(None)
            return
        for source_row in range(self._ui.catalog.model.rowCount()):
            record = self._ui.catalog.model.record(source_row)
            if record and record.meta.identity.video_id == video_id:
                source = self._ui.catalog.model.index(source_row, 0)
                proxy = self._ui.catalog.proxy.mapFromSource(source)
                if proxy.isValid():
                    self._ui.catalog.table.selectRow(proxy.row())
                return

    def _update_actions(self) -> None:
        """Keep process, playback, removal, and cancellation controls honest.

        Example: a corrupt local SRT enables Repair while active work enables Cancel.
        """

        record = self._selected_record()
        issue = library_model.local_pipeline_issue(record) if record else None
        can_process = bool(record and not self._busy and (not record.downloaded or issue or record.download_error))
        can_play = bool(record and record.downloaded)
        self._ui.catalog.detail.set_busy(self._busy)
        needs_repair = bool(record and record.downloaded and (issue or record.download_error))
        self._ui.header.download.setText("↻  Repair" if needs_repair else "↓  Download")
        self._ui.header.download.setEnabled(can_process)
        self._ui.header.play.setEnabled(can_play)
        self._ui.header.check.setEnabled(not self._busy)
        self._ui.header.cancel.setVisible(self._busy)
        task = self._active_task
        self._ui.header.cancel.setEnabled(bool(task and not task.cancel_requested))
        self._cancel_action.setEnabled(bool(task and not task.cancel_requested))
        self._remove_action.setEnabled(bool(record and record.downloaded and not self._busy))
