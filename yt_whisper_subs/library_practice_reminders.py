"""Daily practice eligibility and durable snoozes for the tray notification.

Example: dismissing Friday's reminder snoozes for four real hours, even across a restart.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import NamedTuple, TYPE_CHECKING

from PySide6 import QtCore
from PySide6 import QtWidgets

from yt_whisper_subs import i18n

if TYPE_CHECKING:
    from yt_whisper_subs import library_db
    from yt_whisper_subs import practice_activity


SNOOZE_HOURS = (1, 72)
MIN_PRACTICE_HOURS = 24


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

        super().__init__(i18n.tr('Daily practice reminder'))
        self.setObjectName("practiceReminderSettings")
        self._enabled = QtWidgets.QCheckBox(i18n.tr("Remind me on days I haven't practiced"))
        self._enabled.setObjectName("practiceReminderEnabled")
        self._enabled.setChecked(prefs.enabled)
        self._hours = QtWidgets.QSpinBox()
        self._hours.setObjectName("practiceSnoozeHours")
        self._hours.setRange(*SNOOZE_HOURS)
        self._hours.setSuffix(" h")
        self._hours.setValue(prefs.snooze_hours)
        self._hours.setEnabled(prefs.enabled)
        self._enabled.toggled.connect(self._hours.setEnabled)
        note = QtWidgets.QLabel(i18n.tr(
            'Closing the balloon snoozes it. Submitted answers keep reminders quiet for at least '
            '{MIN_PRACTICE_HOURS} hours. Reminders run while the library is open or in the system '
            'tray.',
            MIN_PRACTICE_HOURS=MIN_PRACTICE_HOURS,
        ))
        note.setWordWrap(True)
        note.setObjectName("settingsNote")
        form = QtWidgets.QFormLayout(self)
        form.addRow(self._enabled)
        form.addRow(i18n.tr('Snooze for'), self._hours)
        form.addRow(note)

    def values(self) -> ReminderPrefs:
        """Return the edited policy; e.g. Cancel leaves the persisted deadline and controls alone."""

        return ReminderPrefs(self._enabled.isChecked(), self._hours.value())


class Reminder(NamedTuple):
    """Carry the displayed reminder text and chart inputs, e.g. a week of counts and a four-hour snooze."""

    activity: practice_activity.Activity
    snooze_hours: int


class PracticeNotice(QtWidgets.QWidget):
    """Offer a short round and seven daily counts inside the shared balloon."""

    requested = QtCore.Signal()
    snoozed = QtCore.Signal()

    def __init__(self) -> None:
        """Load the chart only when a reminder is actually shown; e.g. keep ordinary library startup lean."""

        from yt_whisper_subs import library_practice_activity

        super().__init__()
        self._heading = QtWidgets.QLabel(i18n.tr('Time for practice'))
        self._heading.setObjectName("notificationHeading")
        self._message = QtWidgets.QLabel()
        self._message.setWordWrap(True)
        self._chart = library_practice_activity.ActivityChart()
        actions = QtWidgets.QHBoxLayout()
        self._snooze = QtWidgets.QPushButton()
        self._snooze.setObjectName("snoozePractice")
        self._snooze.clicked.connect(self.snoozed)
        start = QtWidgets.QPushButton(i18n.tr('Practice now'))
        start.setObjectName("primaryButton")
        start.clicked.connect(self.requested)
        actions.addStretch()
        actions.addWidget(self._snooze)
        actions.addWidget(start)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._heading)
        layout.addWidget(self._message)
        layout.addWidget(self._chart, 1)
        layout.addLayout(actions)

    def render(self, reminder: Reminder, *, with_videos: bool) -> None:
        """Show the practice heading inline when the main heading describes new videos."""

        activity, hours = reminder
        self._heading.setVisible(with_videos)
        message = i18n.tr('A short round with your saved words?')
        if activity.gap is not None and activity.gap >= 2:
            message = i18n.tr("It's been {gap} days since you last practiced.", gap=activity.gap)
        elif activity.gap is None:
            message = i18n.tr('Your saved words are ready for your first practice.')
        self._message.setText(message)
        self._chart.set_days(activity.week)
        self._snooze.setText(i18n.tr('Snooze {snooze_hours}h', snooze_hours=hours))


class PracticeReminders:
    """Combine elapsed-time spacing with daily credit; e.g. a late session stays credited across midnight and restart."""

    def __init__(self, db: library_db.LibraryDb, *, clock: Callable[[], float] = time.time) -> None:
        """Use an injectable wall clock; e.g. test a four-hour snooze without waiting."""

        self._db, self._clock = db, clock
        self._prefs = ReminderPrefs.load(db)
        self._cache_key = None
        self._cached: Reminder | None = None

    def configure(self, prefs: ReminderPrefs) -> None:
        """Apply enablement immediately without rewriting a prior deadline; e.g. change the next snooze to eight hours."""

        self._prefs = prefs

    def due(self) -> Reminder | None:
        """Return a reminder after a full interval and any snooze; e.g. midnight cannot reset a late session."""

        if not self._prefs.enabled:
            return None
        now = self._clock()
        today = datetime.fromtimestamp(now).date()
        last = self._db.practice_last_answer(now)
        # Keep same-day credit on long DST days; elapsed time prevents a midnight reset after a recent answer.
        practiced = last is not None and (
            datetime.fromtimestamp(last).date() == today or now - last < MIN_PRACTICE_HOURS * 3600
        )
        if practiced:
            return None
        try:
            deadline = float(self._db.setting("practice_snooze_until", "0"))
        except ValueError:
            deadline = 0
        if math.isfinite(deadline) and now < deadline:
            return None
        if not self._db.practice_has_candidates() and not self._db.verb_has_history():
            return None
        key = (today, last, self._prefs)
        if self._cache_key != key:
            from yt_whisper_subs import practice_activity

            data = practice_activity.read(self._db, now=datetime.fromtimestamp(now), since=today - timedelta(days=6))
            self._cached = Reminder(data, self._prefs.snooze_hours)
            self._cache_key = key
        return self._cached

    def snooze(self) -> None:
        """Persist an absolute UTC deadline; e.g. four hours survives midnight and a restart."""

        self._db.set_setting("practice_snooze_until", self._clock() + self._prefs.snooze_hours * 3600)
