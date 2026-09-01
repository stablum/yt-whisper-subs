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

        if self._busy or self._metadata_active:
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
        self._pool.start(task)

    def _report_metadata(self, message: str) -> None:
        """Send low-priority maintenance detail to the optional trace only.

        Example: a deferred lookup is visible without disturbing playback status.
        """

        self._ui.trace.append_message(f"Metadata · {message}")

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
        self._pipeline_status_active = False
        self._reported_stage_key = None
        self.statusBar().showMessage(label)
        self._ui.trace.append_message(f"▶ {label}")
        self._update_actions()
        task = library_workers.BackgroundTask(fn)
        task.signals.progress.connect(self._report_progress)

        def done(result: object) -> None:
            """Restore idle state before invoking an operation-specific handler.

            Example: the worker's finished signal invokes `done(result)`.
            """

            self._busy = False
            self._active_task = None
            self._pipeline_status_active = False
            self.statusBar().showMessage("Ready", 3000)
            self._ui.trace.append_message(f"✓ {label}")
            self._update_actions()
            if finished:
                finished(result)
            self._schedule_metadata_backfill()

        def failed(message: str, trace: str) -> None:
            """Show a concise error with optional diagnostic details.

            Example: the worker's failed signal invokes `failed(message, trace)`.
            """

            self._busy = False
            self._active_task = None
            self._pipeline_status_active = False
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
            box.exec()
            self.statusBar().showMessage(f"Error: {message}", 10000)
            self._schedule_metadata_backfill()

        task.signals.finished.connect(done)
        task.signals.failed.connect(failed)
        self._active_task = task
        self._pool.start(task)

    def _report_progress(self, message: str) -> None:
        """Route structured stages to the GUI and raw output to the trace.

        Example: yt-dlp progress updates invoke `_report_progress(message)`.
        """

        if update := progress.parse(message):
            self._pipeline_status_active = True
            self._ui.catalog.model.set_progress(update)
            title = self._ui.catalog.model.title_for(update.video_id)
            percent = ""
            if update.fraction is not None or update.stage in {progress.Stage.READY, progress.Stage.FAILED}:
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
