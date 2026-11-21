"""Coordinate new videos and daily practice in one tray balloon.

Example: `notifier.add(videos)` merges a completed check without per-video alerts.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import NamedTuple

from PySide6 import QtCore
from PySide6 import QtWidgets

from yt_whisper_subs import i18n
from yt_whisper_subs import library_balloon
from yt_whisper_subs import library_db
from yt_whisper_subs import library_practice_reminders
from yt_whisper_subs import library_types as types


class NotificationPrefs(NamedTuple):
    """Keep notification controls and their persisted defaults together.

    Example: `NotificationPrefs(enabled=False)` silences future discoveries.
    """

    enabled: bool = True
    delay_seconds: int = 30
    duration_seconds: int = 20
    only_hidden: bool = False

    @classmethod
    def load(cls, db: library_db.LibraryDb) -> NotificationPrefs:
        """Validate saved controls before they reach timers or widgets.

        Example: an invalid saved delay falls back to thirty seconds.
        """

        defaults = cls()
        values = []
        for name, default in defaults._asdict().items():
            raw = db.setting(f"notify_{name}", str(int(default)))
            if isinstance(default, bool):
                values.append(raw in {"1", "True", "true"})
                continue
            maximum = 3600 if name == "delay_seconds" else 600
            try:
                value = max(0, min(maximum, int(raw)))
            except ValueError:
                value = default
            values.append(value)
        return cls(*values)

    def save(self, db: library_db.LibraryDb) -> None:
        """Use the library's existing settings store for every control.

        Example: saving persists across a quiet tray restart.
        """

        for name, value in self._asdict().items():
            db.set_setting(f"notify_{name}", int(value))


class NotificationSettings(QtWidgets.QGroupBox):
    """Keep notification editing beside its policy instead of growing the main form.

    Example: `controls.values()` returns the enabled state and both timer durations.
    """

    def __init__(self, prefs: NotificationPrefs) -> None:
        """Expose enablement, aggregation, lifetime, and foreground suppression.

        Example: disabling the feature also disables its tuning controls.
        """

        super().__init__(i18n.tr('Pinned-channel notifications'))
        self.setObjectName("notificationSettings")
        form = QtWidgets.QFormLayout(self)
        self._enabled = QtWidgets.QCheckBox(i18n.tr('Notify me about new videos'))
        self._enabled.setObjectName("notifyEnabled")
        self._enabled.setChecked(prefs.enabled)
        self._delay = QtWidgets.QSpinBox()
        self._delay.setObjectName("notifyDelay")
        self._delay.setRange(0, 3600)
        self._delay.setSuffix(" s")
        self._delay.setValue(prefs.delay_seconds)
        self._delay.setToolTip(
            i18n.tr('Collect completed checks for this long, then show one list. Each check always groups all its channels; 0 shows it immediately.')
        )
        self._duration = QtWidgets.QSpinBox()
        self._duration.setObjectName("notifyDuration")
        self._duration.setRange(0, 600)
        self._duration.setSuffix(" s")
        self._duration.setSpecialValueText(i18n.tr('Until dismissed'))
        self._duration.setValue(prefs.duration_seconds)
        self._duration.setToolTip(i18n.tr('Reading time pauses while the pointer is over the balloon.'))
        self._hidden = QtWidgets.QCheckBox(i18n.tr('Only while hidden or minimized'))
        self._hidden.setObjectName("notifyOnlyHidden")
        self._hidden.setChecked(prefs.only_hidden)
        for control in (self._delay, self._duration, self._hidden):
            control.setEnabled(prefs.enabled)
            self._enabled.toggled.connect(control.setEnabled)
        form.addRow(self._enabled)
        form.addRow(i18n.tr('Group arrivals for'), self._delay)
        form.addRow(i18n.tr('Dismiss after'), self._duration)
        form.addRow(self._hidden)

    def values(self) -> NotificationPrefs:
        """Return one cohesive value for saving and immediate runtime updates.

        Example: zero dismissal seconds keeps the balloon open until closed.
        """

        return NotificationPrefs(
            self._enabled.isChecked(), self._delay.value(),
            self._duration.value(), self._hidden.isChecked(),
        )


class TrayNotifications(QtCore.QObject):
    """Keep one balloon current as video batches and practice reminders become due."""

    video_requested = QtCore.Signal(str)
    settings_requested = QtCore.Signal()
    practice_requested = QtCore.Signal(bool)

    def __init__(
        self, db: library_db.LibraryDb, tray: QtWidgets.QSystemTrayIcon, *,
        practicing: Callable[[], bool] = lambda: False,
    ) -> None:
        """Tie both schedules and popup actions to the tray's lifetime."""

        super().__init__(tray)
        self._db = db
        self._tray = tray
        self._prefs = NotificationPrefs.load(db)
        self._pending: dict[str, types.VideoMeta] = {}
        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._show_pending)
        self._stopped = False
        self._practicing = practicing
        self._practice = library_practice_reminders.PracticeReminders(db)
        self._reminder: library_practice_reminders.Reminder | None = None
        self._balloon = library_balloon.TrayBalloon(tray.parent())
        self._balloon.dismissed.connect(self._dismiss)
        self._balloon.hidden.connect(self._clear_displayed)
        self._balloon.videos_expired.connect(self._expire_videos)
        self._balloon.video_requested.connect(self._open_video)
        self._balloon.settings_requested.connect(self._open_settings)
        self._balloon.practice_requested.connect(self._open_practice)
        self._balloon.practice_snoozed.connect(self.snooze_practice)
        self._practice_timer = QtCore.QTimer(self)
        self._practice_timer.setInterval(60_000)
        self._practice_timer.timeout.connect(self.check_practice)
        self._practice_timer.start()
        QtCore.QTimer.singleShot(5000, self, self.check_practice)

    @QtCore.Slot(object)
    def add(self, videos: list[types.VideoMeta]) -> None:
        """Start a bounded grouping window or extend the visible video list."""

        if self._stopped or not self._prefs.enabled or not videos:
            return
        updating = bool(self._pending) and not self._timer.isActive()
        for meta in videos:
            self._pending[meta.identity.video_id] = meta
        if updating:
            self._show_pending()
        else:
            self._retain_pinned()
            if self._pending and not self._timer.isActive():
                self._timer.start(self._prefs.delay_seconds * 1000)

    def configure(self, prefs: NotificationPrefs) -> None:
        """Apply video settings immediately; e.g. turning off alerts removes the video list."""

        self._prefs = prefs
        if not prefs.enabled:
            self._timer.stop()
            self._pending.clear()
            self._render()
        elif self._timer.isActive():
            self._timer.start(prefs.delay_seconds * 1000)
        elif self._pending:
            self._show_pending()

    def configure_practice(self, prefs: library_practice_reminders.ReminderPrefs) -> None:
        """Apply reminder settings to the practice section as soon as they are saved."""

        self._practice.configure(prefs)
        self.check_practice()

    @QtCore.Slot()
    def check_practice(self) -> None:
        """Refresh practice when due or answered; e.g. append it to a visible video list."""

        if not self._stopped and self._sync_practice():
            self._render()

    @QtCore.Slot()
    def snooze_practice(self) -> None:
        """Postpone a visible reminder while keeping any new videos readable."""

        if self._reminder is not None:
            self._practice.snooze()
            self._reminder = None
            self._render()

    def stop(self) -> None:
        """Prevent late signals from reopening the balloon during shutdown."""

        self._stopped = True
        self._timer.stop()
        self._practice_timer.stop()
        self._pending.clear()
        self._reminder = None
        self._balloon.hide()

    def _retain_pinned(self) -> None:
        """Recheck membership before delivery; e.g. exclude channels unpinned during the delay."""

        pinned = {channel.channel_id for channel in self._db.channels() if channel.pinned}
        self._pending = {
            video_id: meta for video_id, meta in self._pending.items()
            if (record := self._db.video(video_id)) and record.subscription_id in pinned
        }

    @QtCore.Slot()
    def _show_pending(self) -> None:
        """Deliver the finished video batch together with any reminder that is due."""

        if self._stopped:
            return
        self._timer.stop()
        self._retain_pinned()
        owner = self._balloon.parentWidget()
        if self._prefs.only_hidden and owner.isVisible() and not owner.isMinimized():
            self._pending.clear()
        self._sync_practice()
        self._render()

    def _sync_practice(self) -> bool:
        """Check whether practice content changed before redrawing the balloon."""

        if self._timer.isActive() and self._reminder is None:
            # Let a new reminder join the video alert when its grouping window ends.
            return False
        modal = QtWidgets.QApplication.activeModalWidget() is not None
        reminder = None if self._practicing() or modal else self._practice.due()
        changed = reminder is not self._reminder
        self._reminder = reminder
        return changed

    def _render(self) -> None:
        """Show ready content only; e.g. snoozing cannot release a batch still collecting."""

        videos = () if self._timer.isActive() else self._pending.values()
        if videos or self._reminder is not None:
            self._balloon.present(videos, self._reminder, self._tray.geometry(), self._prefs.duration_seconds)
        else:
            self._balloon.hide()

    def _expire_videos(self) -> None:
        """Consume the timed video section while leaving a due reminder in place."""

        self._pending.clear()
        self._sync_practice()
        self._render()

    def _dismiss(self) -> None:
        """Dismiss the visible content and snooze practice when it was included."""

        if self._reminder is not None:
            self._practice.snooze()
        self._balloon.hide()

    def _clear_displayed(self) -> None:
        """Consume displayed content on hide; e.g. keep a video batch still in its grouping window."""

        self._reminder = None
        if not self._timer.isActive():
            self._pending.clear()

    def _open_video(self, video_id: str) -> None:
        """Close the balloon before selecting a title or opening the pinned list."""

        self._dismiss()
        self.video_requested.emit(video_id)

    def _open_practice(self, activity: bool) -> None:
        """Dismiss and snooze before opening Practice or its activity calendar."""

        self._dismiss()
        self.practice_requested.emit(activity)

    def _open_settings(self) -> None:
        """Open the shared settings without obscuring it with the balloon."""

        self._dismiss()
        self.settings_requested.emit()
