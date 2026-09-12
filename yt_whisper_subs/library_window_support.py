"""Runtime mixin for library scheduling, tasks, tray lifetime, and shutdown.

Example: `LibraryWindow(WindowRuntimeMixin, QMainWindow)` reuses this policy.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from PySide6 import QtCore
from PySide6 import QtGui
from PySide6 import QtWidgets

from yt_whisper_subs import cfg
from yt_whisper_subs import library_workers
from yt_whisper_subs import playback_progress
from yt_whisper_subs import pipeline_progress as progress


class WindowRuntimeMixin:
    """Separate periodic and asynchronous runtime behavior from GUI layout.

    Example: `LibraryWindow` mixes this concern into the native main window.
    """

    def _build_tray(self) -> QtWidgets.QSystemTrayIcon:
        """Create the system-tray lifetime needed for unattended checks.

        Example: closing the window leaves `_build_tray()` visible by default.
        """

        icon = self.style().standardIcon(QtWidgets.QStyle.StandardPixmap.SP_MediaPlay)
        self.setWindowIcon(icon)
        tray = QtWidgets.QSystemTrayIcon(icon, self)
        tray.setToolTip("yt-whisper-subs YouTube Library")
        menu = QtWidgets.QMenu()
        menu.addAction("Show library", self._show_window)
        menu.addAction("Check now", self.check_now)
        menu.addSeparator()
        menu.addAction("Quit", self._quit)
        tray.setContextMenu(menu)
        tray.activated.connect(self._tray_activated)
        if QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
            tray.show()
        return tray

    def check_now(self) -> None:
        """Start a manual all-channel discovery check.

        Example: the toolbar and tray both invoke `check_now()`.
        """

        self._run_task("Checking channels…", self._service.check_all, self._check_finished)

    def _check_finished(self, new_count: object) -> None:
        """Refresh, reschedule, and optionally notify after a library check.

        Example: background completion invokes `_check_finished(result)`.
        """

        self.refresh()
        self._schedule_next()
        count = int(new_count or 0)
        self.statusBar().showMessage(f"Check complete · {count} new auto-download candidate(s)", 8000)
        if not self.isVisible() and count:
            self._tray.showMessage("YouTube Library updated", f"Found {count} new video(s).")

    def _scheduled_check(self) -> None:
        """Run an overdue check or defer briefly when another task is active.

        Example: the single-shot scheduler calls `_scheduled_check()`.
        """

        if self._busy:
            self._timer.start(5 * 60 * 1000)
            return
        self.check_now()

    def _schedule_next(self, *, initial: bool = False) -> None:
        """Schedule against the last completed check and configured interval.

        Example: `_schedule_next(initial=True)` catches up after app launch.
        """

        hours = float(self._service.db.setting("check_hours", str(cfg.DEFAULT_LIBRARY_CHECK_HOURS)))
        interval = max(900, round(hours * 3600))
        last_check = int(float(self._service.db.setting("last_check_at", "0")))
        now = int(time.time())
        due = last_check + interval if last_check else now + (1 if initial else interval)
        delay = max(1, due - now)
        self._timer.start(delay * 1000)
        when = datetime.fromtimestamp(now + delay).astimezone().strftime("%a %H:%M")
        self._ui.next_check.setText(f"Next check: {when}")

    def _schedule_metadata_backfill(self, *, idle: bool = False) -> None:
        """Pace metadata work independently from bursty channel checks.

        Example: task completion schedules one lookup for a later timer tick.
        """

        has_work = self._service.db.metadata_backlog_count() > 0
        seconds = cfg.DEFAULT_LIBRARY_METADATA_PACE_SECONDS
        if idle or not has_work:
            seconds = cfg.DEFAULT_LIBRARY_METADATA_IDLE_SECONDS
        self._metadata_timer.start(seconds * 1000)

    def _metadata_tick(self) -> None:
        """Run one quiet metadata lookup only while foreground work is idle.

        Example: `_metadata_timer` invokes this at the configured safe pace.
        """

        if self._busy or self._channel_tasks or self._metadata_active:
            self._schedule_metadata_backfill()
            return
        self._metadata_active = True
        task = library_workers.BackgroundTask(self._service.backfill_metadata)
        task.signals.progress.connect(self._report_metadata)

        def done(result: object) -> None:
            """Refresh the queue after one attempted or empty maintenance tick.

            Example: a successful metadata worker invokes `done(result)`.
            """

            self._metadata_active = False
            self._metadata_task = None
            self.refresh()
            attempted = bool(getattr(result, "attempted", False))
            completed = bool(getattr(result, "completed", False))
            idle = not attempted or not completed
            if attempted and not completed:
                minutes = cfg.DEFAULT_LIBRARY_METADATA_IDLE_SECONDS // 60
                self._ui.trace.append_message(
                    f"Metadata · Queue paused for {minutes} minutes after a failed lookup"
                )
            self._schedule_metadata_backfill(idle=idle)

        def failed(message: str, trace: str) -> None:
            """Keep maintenance failures non-modal and retry them gently.

            Example: an unexpected database error invokes `failed(message, trace)`.
            """

            self._metadata_active = False
            self._metadata_task = None
            self._ui.trace.append_message(f"✗ Metadata maintenance · {message}")
            self._ui.trace.append_message(trace)
            self._schedule_metadata_backfill(idle=True)

        task.signals.finished.connect(done)
        task.signals.failed.connect(failed)
        self._metadata_task = task
        self._pool.start(task, -1)

    def _report_metadata(self, message: str) -> None:
        """Send low-priority maintenance detail to the optional trace only.

        Example: a deferred lookup is visible without disturbing playback status.
        """

        self._ui.trace.append_message(f"Metadata · {message}")

    def _queue_channel_task(
        self,
        label: str,
        fn: Callable[[Callable[[str], None]], Any],
    ) -> None:
        """Queue channel hydration behind work in the single execution lane.

        Example: `@ruis` appears now and resolves after the active download.
        """

        self._metadata_timer.stop()
        queued = self._busy or bool(self._channel_tasks)
        task = library_workers.BackgroundTask(fn)
        task_id = id(task)
        self._channel_tasks[task_id] = task
        task.signals.progress.connect(self._report_channel)
        if queued:
            self._ui.trace.append_message(f"○ Channel queued · {label}")

        def started() -> None:
            """Mark the moment this channel reaches its serial worker slot.

            Example: the second queued subscription starts after the first.
            """

            self._ui.trace.append_message(f"▶ {label}")

        def done(_result: object) -> None:
            """Refresh the hydrated sidebar without disturbing pipeline state.

            Example: a resolved YouTube title replaces the saved placeholder.
            """

            self._channel_tasks.pop(task_id, None)
            self._ui.trace.append_message(f"✓ {label}")
            self.refresh()
            if not self._busy:
                self.statusBar().showMessage("Channel added", 3_000)
            self._schedule_metadata_backfill()

        def failed(message: str, trace: str) -> None:
            """Expose a channel error without changing the active video task.

            Example: a failed lookup leaves its visible sidebar placeholder.
            """

            self._channel_tasks.pop(task_id, None)
            self.refresh()
            self._ui.trace.append_message(f"✗ {label} · {message}")
            self._ui.trace.append_message(trace)
            box = QtWidgets.QMessageBox(
                QtWidgets.QMessageBox.Icon.Warning,
                "Could not initialize channel",
                message,
                parent=self,
            )
            box.setDetailedText(trace)
            box.open()
            if not self._busy:
                self.statusBar().showMessage(f"Channel error: {message}", 10_000)
            self._schedule_metadata_backfill(idle=True)

        task.signals.started.connect(started)
        task.signals.finished.connect(done)
        task.signals.failed.connect(failed)
        self._pool.start(task)

    def _report_channel(self, message: str) -> None:
        """Trace channel discovery without overwriting video progress wording.

        Example: yt-dlp channel output appears as `Channel · ...` in the trace.
        """

        self._ui.trace.append_message(f"Channel · {message}")

    def _run_task(
        self,
        label: str,
        fn: Callable[[Callable[[str], None]], Any],
        finished: Callable[[object], None] | None = None,
    ) -> None:
        """Serialize background work and centralize progress/error UX.

        Example: `_run_task("Checking…", service.check_all, handler)`.
        """

        if self._busy:
            self.statusBar().showMessage("Another library task is already running", 5000)
            return
        self._metadata_timer.stop()
        self._busy = True
        self._active_progress = None
        self._paused_progress = None
        self._pipeline_status_active = False
        self._reported_stage_key = None
        self.statusBar().showMessage(label)
        self._ui.trace.append_message(f"▶ {label}")
        task = library_workers.BackgroundTask(fn)
        task.signals.progress.connect(self._report_progress)

        def done(result: object) -> None:
            """Restore idle state before invoking an operation-specific handler.

            Example: the worker's finished signal invokes `done(result)`.
            """

            self._busy = False
            self._active_task = None
            self._pipeline_status_active = False
            self._active_progress = None
            self._paused_progress = None
            self._release_video_slot()
            self.statusBar().showMessage("Ready", 3000)
            self._ui.trace.append_message(f"✓ {label}")
            if finished:
                finished(result)
            self._continue_video_queue()

        def failed(message: str, trace: str) -> None:
            """Show a concise error with optional diagnostic details.

            Example: the worker's failed signal invokes `failed(message, trace)`.
            """

            self._busy = False
            self._active_task = None
            self._pipeline_status_active = False
            self._active_progress = None
            self._paused_progress = None
            self._release_video_slot()
            self.refresh()
            self._ui.trace.append_message(f"✗ {label} · {message}")
            self._ui.trace.append_message(trace)
            box = QtWidgets.QMessageBox(
                QtWidgets.QMessageBox.Icon.Critical,
                "Library task failed",
                message,
                parent=self,
            )
            box.setDetailedText(trace)
            box.open()
            self.statusBar().showMessage(f"Error: {message}", 10000)
            self._continue_video_queue()

        task.signals.finished.connect(done)
        task.signals.failed.connect(failed)

        def cancelled() -> None:
            """Restore idle state without presenting a requested stop as failure.

            Example: stopping Whisper leaves an amber Cancelled row and no dialog.
            """

            update = self._active_progress
            self._busy = False
            self._active_task = None
            self._pipeline_status_active = False
            self._release_video_slot()
            self.refresh()
            if update:
                cancelled_update = progress.make(
                    update.video_id,
                    progress.Stage.CANCELLED,
                    progress.overall_fraction(update),
                )
                self._ui.catalog.model.set_progress(cancelled_update)
            self._active_progress = None
            self._paused_progress = None
            self.statusBar().showMessage("Operation cancelled", 8_000)
            self._ui.trace.append_message(f"■ Cancelled · {label}")
            self._continue_video_queue()

        task.signals.cancelled.connect(cancelled)
        self._active_task = task
        self._update_actions()
        self._pool.start(task)

    def _pause_active_task(self) -> None:
        """Toggle suspension of the active video pipeline and its process tree.

        Example: Ctrl+Shift+P freezes and later continues Whisper in memory.
        """

        task = self._active_task
        update = self._active_progress
        if not task or not update:
            return
        if task.paused:
            if not task.resume():
                return
            self._service.set_pipeline_paused(update.video_id, False)
            original = self._paused_progress or update
            self._active_progress = original
            resuming = progress.make(
                original.video_id,
                original.stage,
                original.fraction,
                f"Resuming · {progress.stage_label(original.stage)}",
            )
            self._ui.catalog.model.set_progress(resuming)
            self.statusBar().showMessage(resuming.label)
            self._ui.trace.append_message("▶ Pipeline resumed")
        else:
            if not task.pause():
                return
            self._service.set_pipeline_paused(update.video_id, True)
            self._paused_progress = update
            paused = progress.make(
                update.video_id,
                progress.Stage.PAUSED,
                progress.overall_fraction(update),
                f"Paused · {progress.stage_label(update.stage)}",
            )
            self._ui.catalog.model.set_progress(paused)
            self.statusBar().showMessage(paused.label)
            self._ui.trace.append_message("Ⅱ Pipeline paused")
        self._update_actions()

    def _restore_interrupted_pipelines(self) -> None:
        """Expose unclean prior work as a one-click resumable table state.

        Example: a system crash leaves an amber Resume row after restart.
        """

        jobs = self._service.recover_pipeline_jobs()
        for job in jobs:
            label = f"Interrupted · {job.label} · click Resume"
            interrupted = progress.make(
                job.video_id,
                progress.Stage.INTERRUPTED,
                job.fraction,
                label,
            )
            self._ui.catalog.model.set_progress(interrupted)
        if jobs:
            self._ui.trace.append_message(
                f"Recovery · {len(jobs)} interrupted pipeline(s) ready to resume"
            )
            self._update_actions()

    def _cancel_active_task(self) -> None:
        """Request cancellation of the active heavy task and its child tools.

        Example: Ctrl+Shift+X stops yt-dlp, ffmpeg, Whisper, or translation.
        """

        task = self._active_task
        if not task or not task.cancel():
            return
        self.statusBar().showMessage("Cancelling current operation…")
        self._ui.trace.append_message("■ Cancellation requested")
        if update := self._active_progress:
            cancelling = progress.make(
                update.video_id,
                update.stage,
                update.fraction,
                f"Cancelling · {progress.stage_label(update.stage)}",
            )
            self._ui.catalog.model.set_progress(cancelling)
        self._update_actions()

    def _run_playback(
        self,
        label: str,
        fn: Callable[[Callable[[str], None]], Any],
        finished: Callable[[object], None] | None = None,
    ) -> None:
        """Launch mpv outside the serialized compute and network lane.

        Example: _run_playback("Opening mpv…", service.play) works during Whisper.
        """

        task = library_workers.BackgroundTask(fn)
        task_id = id(task)
        self._playback_tasks[task_id] = task
        self._ui.trace.append_message(f"▶ {label}")
        if not self._busy:
            self.statusBar().showMessage(label)

        def done(result: object) -> None:
            """Release one player worker after its visible mpv process exits.

            Example: closing mpv invokes done(None) and refreshes watched state.
            """

            self._playback_tasks.pop(task_id, None)
            self._ui.trace.append_message(f"✓ {label}")
            if not self._busy:
                self.statusBar().showMessage("Ready", 3_000)
            if finished:
                finished(result)

        def failed(message: str, trace: str) -> None:
            """Expose a playback-only failure without disturbing active work.

            Example: missing mpv opens a non-blocking error while Whisper continues.
            """

            self._playback_tasks.pop(task_id, None)
            self._ui.trace.append_message(f"✗ {label} · {message}")
            self._ui.trace.append_message(trace)
            box = QtWidgets.QMessageBox(
                QtWidgets.QMessageBox.Icon.Critical,
                "Playback failed",
                message,
                parent=self,
            )
            box.setDetailedText(trace)
            box.open()
            if not self._busy:
                self.statusBar().showMessage(f"Playback error: {message}", 10_000)

        task.signals.progress.connect(self._report_playback)
        task.signals.finished.connect(done)
        task.signals.failed.connect(failed)
        self._playback_pool.start(task)

    def _report_playback(self, message: str) -> None:
        """Update watched state without overwriting an active pipeline stage.

        Example: mpv progress updates its row while translation remains in the status bar.
        """

        if watched := playback_progress.parse(message):
            self._show_watched_progress(watched, update_status=not self._busy)
            return
        if not self._busy:
            self.statusBar().showMessage(message)
        self._ui.trace.append_message(f"Playback · {message}")

    def _show_watched_progress(
        self,
        watched: playback_progress.Update,
        *,
        update_status: bool,
    ) -> None:
        """Render one mpv observation with optional status-bar ownership.

        Example: concurrent playback updates its row without hiding Whisper's label.
        """

        self._ui.catalog.model.set_watched_progress(watched)
        title = self._ui.catalog.model.title_for(watched.video_id)
        fraction = playback_progress.fraction(watched)
        label = "Watched" if watched.completed else f"Watching · {fraction:.0%}"
        if update_status:
            self.statusBar().showMessage(f"{label} · {title}")
        if watched.completed:
            self._ui.trace.append_message(f"◆ {title} · Reached end · 100% watched")

    def _report_progress(self, message: str) -> None:
        """Route structured stages to the GUI and raw output to the trace.

        Example: yt-dlp progress updates invoke `_report_progress(message)`.
        """

        if watched := playback_progress.parse(message):
            self._show_watched_progress(watched, update_status=True)
            return
        if update := progress.parse(message):
            first_pipeline_update = self._active_progress is None
            self._pipeline_status_active = True
            self._active_progress = update
            self._ui.catalog.model.set_progress(update)
            if first_pipeline_update:
                self._update_video_queue()
                self._update_actions()
            title = self._ui.catalog.model.title_for(update.video_id)
            percent = ""
            terminal = {progress.Stage.READY, progress.Stage.FAILED, progress.Stage.CANCELLED}
            if update.fraction is not None or update.stage in terminal:
                percent = f" · {progress.overall_fraction(update):.0%} overall"
            self.statusBar().showMessage(f"{update.label}{percent} · {title}")
            stage_key = (update.video_id, update.stage)
            if stage_key != self._reported_stage_key:
                self._ui.trace.append_message(f"◆ {title} · {update.label}")
                self._reported_stage_key = stage_key
            return
        if not getattr(self, "_pipeline_status_active", False):
            self.statusBar().showMessage(message)
        self._ui.trace.append_message(message)

    def _show_window(self) -> None:
        """Restore and focus the library from its system-tray action.

        Example: the tray's Show action invokes `_show_window()`.
        """

        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _tray_activated(self, reason: QtWidgets.QSystemTrayIcon.ActivationReason) -> None:
        """Restore the app on normal tray-icon activation.

        Example: a tray double-click invokes `_tray_activated(reason)`.
        """

        if reason in {
            QtWidgets.QSystemTrayIcon.ActivationReason.Trigger,
            QtWidgets.QSystemTrayIcon.ActivationReason.DoubleClick,
        }:
            self._show_window()

    def _quit(self) -> None:
        """Exit explicitly even when minimize-to-tray is enabled.

        Example: Library → Quit invokes `_quit()`.
        """

        self._quitting = True
        QtWidgets.QApplication.quit()

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        """Hide to the tray when configured so four-hour checks continue.

        Example: the window close button invokes `closeEvent(event)`.
        """

        minimize = self._service.db.setting(
            "minimize_to_tray",
            str(int(cfg.DEFAULT_LIBRARY_MINIMIZE_TO_TRAY)),
        ) in {"1", "True", "true"}
        if not self._quitting and minimize and self._tray.isVisible():
            event.ignore()
            self.hide()
            if self._service.db.setting("tray_hint_shown", "0") != "1":
                self._tray.showMessage(
                    "YouTube Library is still running",
                    "Automatic channel checks continue in the tray.",
                )
                self._service.db.set_setting("tray_hint_shown", 1)
            return
        event.accept()
        if not self._quitting:
            self._quitting = True
            QtCore.QTimer.singleShot(0, QtWidgets.QApplication.quit)
