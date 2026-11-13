"""Daily practice reminders with durable snoozes and the shared activity chart.

Example: dismissing Friday's reminder snoozes for four real hours, even across a restart.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import NamedTuple

from PySide6 import QtCore
from PySide6 import QtWidgets

from yt_whisper_subs import library_balloon


SNOOZE_HOURS = (1, 72)


class ReminderPrefs(NamedTuple):
    """Keep daily-reminder controls separate from video alerts; e.g. disable only the practice reminder."""

    enabled: bool = True
    snooze_hours: int = 4

    @classmethod
    def load(cls, db) -> ReminderPrefs:
        """Validate persisted settings before using them; e.g. a malformed snooze falls back to four hours."""

        defaults = cls()
        enabled = db.setting("practice_reminder_enabled", str(int(defaults.enabled))) in {"1", "true", "True"}
        try:
            hours = int(db.setting("practice_snooze_hours", str(defaults.snooze_hours)))
            hours = max(SNOOZE_HOURS[0], min(SNOOZE_HOURS[1], hours))
        except ValueError:
            hours = defaults.snooze_hours
        return cls(enabled, hours)

    def save(self, db) -> None:
        """Persist controls in the existing catalog; e.g. Save applies the next snooze duration."""

        db.set_setting("practice_reminder_enabled", int(self.enabled))
        db.set_setting("practice_snooze_hours", self.snooze_hours)


class ReminderSettings(QtWidgets.QGroupBox):
    """Expose reminder enablement and snooze alongside existing notifications; e.g. choose six hours."""

    def __init__(self, prefs: ReminderPrefs) -> None:
        """Keep dismissal semantics visible in the form; e.g. close and Escape also snooze."""

        super().__init__("Daily practice reminder")
        self.setObjectName("practiceReminderSettings")
        self._enabled = QtWidgets.QCheckBox("Remind me on days I haven't practiced")
        self._enabled.setObjectName("practiceReminderEnabled")
        self._enabled.setChecked(prefs.enabled)
        self._hours = QtWidgets.QSpinBox()
        self._hours.setObjectName("practiceSnoozeHours")
        self._hours.setRange(*SNOOZE_HOURS)
        self._hours.setSuffix(" hours")
        self._hours.setValue(prefs.snooze_hours)
        self._hours.setEnabled(prefs.enabled)
        self._enabled.toggled.connect(self._hours.setEnabled)
        note = QtWidgets.QLabel("Closing the balloon snoozes it. Answer a question to finish today's reminder. "
                               "Reminders run while the library is open or in the system tray.")
        note.setWordWrap(True)
        note.setObjectName("settingsNote")
        form = QtWidgets.QFormLayout(self)
        form.addRow(self._enabled)
        form.addRow("Snooze for", self._hours)
        form.addRow(note)

    def values(self) -> ReminderPrefs:
        """Return the edited policy; e.g. Cancel leaves the persisted deadline and controls alone."""

        return ReminderPrefs(self._enabled.isChecked(), self._hours.value())


class PracticeBalloon(library_balloon.TrayBalloon):
    """Offer a short round and seven visible daily counts; e.g. show a three-day practice gap."""

    requested = QtCore.Signal(bool)
    settings_requested = QtCore.Signal()

    def __init__(self, parent) -> None:
        """Load the chart only when a reminder is actually shown; e.g. keep ordinary library startup lean."""

        from yt_whisper_subs import library_practice_activity

        super().__init__("Time for practice", parent)
        self.setObjectName("practiceBalloon")
        self._message = QtWidgets.QLabel()
        self._message.setWordWrap(True)
        self._chart = library_practice_activity.ActivityChart()
        links = QtWidgets.QHBoxLayout()
        activity = QtWidgets.QPushButton("Activity && calendar")
        activity.setObjectName("openPracticeActivity")
        activity.clicked.connect(lambda: self.requested.emit(True))
        settings = QtWidgets.QPushButton("Settings…")
        settings.clicked.connect(self.settings_requested)
        links.addWidget(activity)
        links.addStretch()
        links.addWidget(settings)
        actions = QtWidgets.QHBoxLayout()
        self._snooze = QtWidgets.QPushButton()
        self._snooze.setObjectName("snoozePractice")
        self._snooze.clicked.connect(self.reject)
        start = QtWidgets.QPushButton("Practice now")
        start.setObjectName("primaryButton")
        start.clicked.connect(lambda: self.requested.emit(False))
        actions.addWidget(self._snooze)
        actions.addStretch()
        actions.addWidget(start)
        self._root.addWidget(self._message)
        self._root.addWidget(self._chart, 1)
        self._root.addLayout(links)
        self._root.addLayout(actions)

    def present(self, activity, prefs: ReminderPrefs, anchor: QtCore.QRect) -> None:
        """Show elapsed local days only when known; e.g. a first-time learner sees no invented inactivity count."""

        message = "A short round with your saved words?"
        if activity.gap is not None and activity.gap >= 2:
            message = f"It's been {activity.gap} days since you last practiced."
        elif activity.gap is None:
            message = "Your saved words are ready for your first practice."
        self._message.setText(message)
        self._chart.set_days(activity.week)
        self._snooze.setText(f"Snooze {prefs.snooze_hours}h")
        close = self.findChild(QtWidgets.QToolButton, "dismissNotification")
        close.setToolTip(f"Snooze for {prefs.snooze_hours} hours (Escape)")
        close.setAccessibleName(f"Snooze practice reminder for {prefs.snooze_hours} hours")
        self._present(anchor, QtCore.QSize(470, 405))


class PracticeReminders(QtCore.QObject):
    """Evaluate local-day eligibility without scheduling OS jobs; e.g. catch up after wake or restart."""

    requested = QtCore.Signal(bool)
    settings_requested = QtCore.Signal()
    hidden = QtCore.Signal()

    def __init__(self, db, tray, available: Callable[[], bool], *, clock: Callable[[], float] = time.time) -> None:
        """Poll cheaply once per minute with an injectable clock; e.g. test a four-hour snooze without waiting."""

        super().__init__(tray)
        self._db, self._tray, self._available, self._clock = db, tray, available, clock
        self._prefs = ReminderPrefs.load(db)
        self._balloon: PracticeBalloon | None = None
        self._display_key = None
        self._stopped = False
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(60_000)
        self._timer.timeout.connect(self.check)
        self._timer.start()
        QtCore.QTimer.singleShot(5000, self, self.check)

    @property
    def visible(self) -> bool:
        """Expose slot occupancy to the notification coordinator; e.g. defer a video balloon until snooze."""

        return self._balloon is not None and self._balloon.isVisible()

    def configure(self, prefs: ReminderPrefs) -> None:
        """Apply enablement immediately without rewriting a prior deadline; e.g. change the next snooze to eight hours."""

        self._prefs = prefs
        self.check()

    def check(self) -> None:
        """Show only when today has no submitted answers and snooze elapsed; e.g. one answer ends today's reminders."""

        if self._stopped:
            return
        now = self._clock()
        today = datetime.fromtimestamp(now).date()
        last = self._db.practice_last_answer(now) if self._prefs.enabled else None
        practiced = last is not None and datetime.fromtimestamp(last).date() == today
        if not self._prefs.enabled or practiced:
            if self._balloon:
                self._balloon.hide()
            return
        try:
            deadline = float(self._db.setting("practice_snooze_until", "0"))
        except ValueError:
            deadline = 0
        if (math.isfinite(deadline) and now < deadline) or not self._available():
            return
        if not self._db.practice_has_candidates():
            return
        key = (today, last, self._prefs)
        if self.visible and self._display_key == key:
            return
        from yt_whisper_subs import practice_activity

        data = practice_activity.read(self._db, now=datetime.fromtimestamp(now), since=today - timedelta(days=6))
        if self._balloon is None:
            self._balloon = PracticeBalloon(self._tray.parent())
            self._balloon.dismissed.connect(self.snooze)
            self._balloon.hidden.connect(self.hidden)
            self._balloon.requested.connect(self._open)
            self._balloon.settings_requested.connect(self._settings)
        self._balloon.present(data, self._prefs, self._tray.geometry())
        self._display_key = key

    def snooze(self) -> None:
        """Persist an absolute UTC deadline before hiding; e.g. four hours survives midnight and a restart."""

        if not self._stopped:
            self._db.set_setting("practice_snooze_until", self._clock() + self._prefs.snooze_hours * 3600)
        if self._balloon:
            self._balloon.hide()

    def defer(self) -> None:
        """Yield the popup slot without snoozing; e.g. show a timely video alert before restoring the reminder."""

        self._display_key = None
        if self._balloon:
            self._balloon.hide()

    def _open(self, activity: bool) -> None:
        """Opening is not practice credit; e.g. a closed unanswered round can be reminded after snooze."""

        self.snooze()
        self.requested.emit(activity)

    def _settings(self) -> None:
        """Make reminder controls directly reachable; e.g. snooze first so the form stays unobscured."""

        self.snooze()
        self.settings_requested.emit()

    def stop(self) -> None:
        """Hide quietly without extending snooze on exit; e.g. a restart should retain the original deadline."""

        self._stopped = True
        self._timer.stop()
        if self._balloon:
            self._balloon.hide()
