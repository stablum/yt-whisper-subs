"""Selection, settings, download, and exact-removal actions for the library window.

Example: `LibraryWindow` mixes these slots into its native Qt shell.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6 import QtCore
from PySide6 import QtGui
from PySide6 import QtWidgets

from yt_whisper_subs import cfg
from yt_whisper_subs import library_artifacts
from yt_whisper_subs import library_types as types
from yt_whisper_subs import library_widgets
from yt_whisper_subs import windows_startup


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
        video_id = record.meta.identity.video_id
        job = self._service.db.pipeline_job(video_id)
        if job and job.state is types.PipelineJobState.INTERRUPTED:

            def resume(report: Callable[[str], None]) -> None:
                """Bind the durable job to the shared idempotent pipeline.

                Example: completed media is reused after a prior system crash.
                """

                self._service.resume_pipeline_job(job, report)

            self._run_task("Resuming interrupted pipeline…", resume, lambda _: self.refresh())
            return
        issue = self._service.pipeline_issue(record)
        if record.downloaded and not issue and not record.download_error:
            self._play_selected()
            return

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
        job = self._service.db.pipeline_job(record.meta.identity.video_id) if record else None
        issue = self._service.pipeline_issue(record) if record else None
        recoverable = bool(job and job.state is types.PipelineJobState.INTERRUPTED)
        if record and record.downloaded and not issue and not record.download_error and not recoverable:
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
        """Persist scheduling, retention, browser, tray, and login settings.

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
            windows_startup.is_enabled(self._service.out_dir),
            recent,
            self._service.db.setting("channel_published_after", ""),
        )
        dialog = library_widgets.SettingsDialog(current, self)
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        try:
            windows_startup.set_enabled(values.start_with_windows, self._service.out_dir)
        except (OSError, RuntimeError) as exc:
            QtWidgets.QMessageBox.warning(
                self,
                "Could not update Windows startup",
                str(exc),
            )
            return
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
        """Filter the resident table for a sidebar library/channel selection.

        Example: Qt passes the new and previous sidebar items.
        """

        del previous
        if current and (key := current.data(QtCore.Qt.ItemDataRole.UserRole)):
            self._filter_key = tuple(key)
            _, channel_id = self._filter_key
            self._ui.catalog.proxy.set_channel(channel_id)
            self._ui.catalog.table.clearSelection()
            self._ui.catalog.detail.set_record(None)
            self._update_actions()

    def _selection_changed(self) -> None:
        """Update detail text and action availability for the selected row.

        Example: table selection changes invoke `_selection_changed()`.
        """

        record = self._selected_record()
        chapter_set = self._service.chapter_set(record.meta.identity.video_id) if record else None
        self._ui.catalog.detail.set_record(record, chapter_set)
        if record:
            video_id = record.meta.identity.video_id
            self._ui.catalog.model.set_issue(video_id, self._service.pipeline_issue(record))
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
        if kind != "channel" or channel_id is None:
            return None
        channels = getattr(self, "_channels_by_id", {})
        return channels.get(channel_id) or self._service.db.channel(channel_id)

    def _restore_video_selection(self, video_id: str | None) -> None:
        """Restore a previous video selection after a model reset when possible.

        Example: `refresh()` calls `_restore_video_selection(id)`.
        """

        if not video_id:
            self._ui.catalog.detail.set_record(None)
            return
        source_row = self._ui.catalog.model.row_for(video_id)
        if source_row is None:
            return
        source = self._ui.catalog.model.index(source_row, 0)
        proxy = self._ui.catalog.proxy.mapFromSource(source)
        if proxy.isValid():
            self._ui.catalog.table.selectRow(proxy.row())

    def _update_actions(self) -> None:
        """Keep process, playback, removal, and cancellation controls honest.

        Example: a corrupt local SRT enables Repair while active work enables Cancel.
        """

        record = self._selected_record()
        model = getattr(self._ui.catalog, "model", None)
        issue = None
        if record:
            issue = (
                model.issue_for(record.meta.identity.video_id)
                if model
                else library_artifacts.pipeline_issue(record)
            )
        job = self._service.db.pipeline_job(record.meta.identity.video_id) if record else None
        recoverable = bool(job and job.state is types.PipelineJobState.INTERRUPTED)
        can_process = bool(
            record
            and not self._busy
            and (recoverable or not record.downloaded or issue or record.download_error)
        )
        can_play = bool(record and record.downloaded)
        self._ui.catalog.detail.set_busy(self._busy)
        needs_repair = bool(record and record.downloaded and (issue or record.download_error))
        process_text = "▶  Resume" if recoverable else ("↻  Repair" if needs_repair else "↓  Download")
        self._ui.header.download.setText(process_text)
        self._ui.header.download.setEnabled(can_process)
        self._ui.header.play.setEnabled(can_play)
        self._ui.header.check.setEnabled(not self._busy)
        pipeline_active = bool(self._busy and self._active_progress and self._active_task)
        paused = bool(self._active_task and self._active_task.paused)
        self._ui.header.pause.setVisible(pipeline_active)
        self._ui.header.pause.setText("▶  Resume" if paused else "Ⅱ  Pause")
        self._ui.header.pause.setEnabled(pipeline_active)
        self._ui.header.cancel.setVisible(self._busy)
        task = self._active_task
        self._ui.header.cancel.setEnabled(bool(task and not task.cancel_requested))
        self._cancel_action.setEnabled(bool(task and not task.cancel_requested))
        self._pause_action.setText("Resume current pipeline" if paused else "Pause current pipeline")
        self._pause_action.setEnabled(pipeline_active)
        self._remove_action.setEnabled(bool(record and record.downloaded and not self._busy))
        pin_action = getattr(self, "_pin_channel_action", None)
        if pin_action:
            channel = self._selected_channel()
            pin_action.setEnabled(channel is not None)
            pin_action.setText("Unpin selected channel" if channel and channel.pinned else "Pin selected channel")

    def _toggle_channel_pin(self) -> None:
        """Move the selected subscription into or out of quick access.

        Example: Channel -> Pin selected channel updates the starred shelf.
        """

        channel = self._selected_channel()
        if not channel:
            QtWidgets.QMessageBox.information(self, "Pinned channels", "Select a tracked channel first.")
            return
        self._service.db.set_channel_pinned(channel.channel_id, not channel.pinned)
        self._refresh_channels()
        self._update_actions()

    def _show_channel_context_menu(self, position: QtCore.QPoint) -> None:
        """Offer direct pinning and subscription controls beside a channel.

        Example: right-clicking a channel exposes Pin without menu hunting.
        """

        widget = self._ui.catalog.channels
        item = widget.itemAt(position)
        if not item or not item.data(QtCore.Qt.ItemDataRole.UserRole):
            return
        widget.setCurrentItem(item)
        channel = self._selected_channel()
        if not channel:
            return
        menu = QtWidgets.QMenu(widget)
        pin = menu.addAction("Unpin from quick access" if channel.pinned else "Pin to quick access")
        pin.triggered.connect(self._toggle_channel_pin)
        auto = menu.addAction(
            "Disable automatic download" if channel.auto_download else "Enable automatic download"
        )
        auto.triggered.connect(self._toggle_channel_auto)
        menu.addSeparator()
        remove = menu.addAction("Stop tracking")
        remove.triggered.connect(self._remove_channel)
        menu.exec(widget.mapToGlobal(position))
