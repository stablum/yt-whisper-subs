"""Test durable snooze, daily eligibility and real native activity widgets.

Example: `python -m unittest tests.test_practice_reminders` uses an injected wall clock.
"""

import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore
from PySide6 import QtTest
from PySide6 import QtWidgets

from tests import practice_fixtures
from tests import verb_fixtures
from tests import test_library_notifications as notification_fixtures
from yt_whisper_subs import library_db
from yt_whisper_subs import library_notifications
from yt_whisper_subs import library_practice_activity
from yt_whisper_subs import library_practice_reminders as reminders
from yt_whisper_subs import library_theme
from yt_whisper_subs import library_types
from yt_whisper_subs import practice
from yt_whisper_subs import practice_activity
from yt_whisper_subs import practice_service
from yt_whisper_subs import verb_service


class ReminderTests(unittest.TestCase):
    """Drive actual dialogs and SQLite with deterministic time; e.g. simulate a restart during snooze."""

    @classmethod
    def setUpClass(cls) -> None:
        """Share one GUI event loop; e.g. Escape must invoke Qt's real dialog dismissal."""

        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self) -> None:
        """Use an isolated vocabulary and tray owner; e.g. the user's real reminder deadlines stay untouched."""

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = library_db.LibraryDb(Path(tmp.name) / "catalog.sqlite3")
        self.db.initialize()
        practice_fixtures.seed(self.db)
        self.engine = practice_service.PracticeService(self.db, practice_fixtures.FakeGenerator())
        self.now = datetime(2026, 11, 13, 12).timestamp()
        self.owner = QtWidgets.QWidget()
        self.owner.setStyleSheet(library_theme.STYLE)
        self.tray = QtWidgets.QSystemTrayIcon(self.owner)
        self.available = True
        self.reminder = reminders.PracticeReminders(self.db, self.tray, lambda: self.available, clock=lambda: self.now)
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        """Stop timers before removing the catalog; e.g. a queued startup check cannot touch a deleted database."""

        self.reminder.stop()
        self.owner.close()
        self.owner.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def _answer(self, at: float, *, count: int = 1, direction=practice.Direction.NL_EN) -> None:
        """Submit real answers at a chosen instant; e.g. distinguish a late session from one a full day ago."""

        opts = practice.RoundOptions(count=count, direction=direction, include_early=True)
        with mock.patch("yt_whisper_subs.practice_db.time.time", return_value=at):
            session = self.engine.start(opts, lambda _: None)
            while row := self.db.practice_current(session):
                self.db.practice_answer(row["id"], row["correct_idx"], False, 1600)

    def test_snooze_recurrence_restart_and_today_completion(self) -> None:
        """Preserve snooze across controller recreation; e.g. no duplicate reminder on a quiet tray restart."""

        self.reminder.check()
        self.assertTrue(self.reminder.visible)
        self.reminder._balloon.findChild(QtWidgets.QPushButton, "snoozePractice").click()
        deadline = float(self.db.setting("practice_snooze_until", "0"))
        self.assertEqual(deadline, self.now + 4 * 3600)
        self.reminder.stop()
        self.reminder = reminders.PracticeReminders(self.db, self.tray, lambda: True, clock=lambda: self.now)
        self.now = deadline - 1
        self.reminder.check()
        self.assertFalse(self.reminder.visible)
        self.now = deadline
        self.reminder.check()
        self.assertTrue(self.reminder.visible)
        self._answer(self.now)
        self.reminder.check()
        self.assertFalse(self.reminder.visible)
        self.now += 2 * 3600
        self.reminder.check()
        self.assertFalse(self.reminder.visible)

    def test_verb_only_history_enables_reminders_and_submitted_verbs_complete_today(self) -> None:
        """Share daily habit accounting across modes; e.g. a saved typed attempt suppresses reminders even if grading is pending."""

        catalog = verb_fixtures.catalog(self.db)
        engine = verb_service.VerbService(self.db, verb_fixtures.FakeVerbProvider())
        with mock.patch.object(self.db, "practice_has_candidates", return_value=False):
            self.reminder.check()
            self.assertFalse(self.reminder.visible)
            with mock.patch.object(self.db, "verb_catalog", return_value=catalog):
                session = engine.start(verb_fixtures.options(1), lambda _: None)
            self.reminder.check()
            self.assertTrue(self.reminder.visible)
            with mock.patch("yt_whisper_subs.verb_db.time.time", return_value=self.now):
                row = self.db.verb_current(session)
                self.db.verb_submit(row["id"], row["content"]["answer"], 1500)
            self.reminder.check()
            self.assertFalse(self.reminder.visible)
            self.now += 2 * 86400
            self.reminder.check()
            self.assertTrue(self.reminder.visible)
            self.assertIn("2 days", self.reminder._balloon._message.text())

    def test_close_escape_and_stop_have_distinct_persistence(self) -> None:
        """User dismissal snoozes but application shutdown does not; e.g. close using the window manager."""

        self.reminder.configure(reminders.ReminderPrefs(snooze_hours=6))
        QtTest.QTest.keyClick(self.reminder._balloon, QtCore.Qt.Key.Key_Escape)
        self.assertEqual(float(self.db.setting("practice_snooze_until", "0")), self.now + 6 * 3600)
        self.now += 6 * 3600
        self.reminder.check()
        self.reminder._balloon.close()
        deadline = self.db.setting("practice_snooze_until", "0")
        self.assertEqual(float(deadline), self.now + 6 * 3600)
        self.now += 6 * 3600
        self.reminder.check()
        self.reminder.stop()
        self.assertEqual(self.db.setting("practice_snooze_until", "0"), deadline)

    def test_gap_and_seven_zero_days_and_never_practiced(self) -> None:
        """Render known gaps without inventing a start date; e.g. an empty vocabulary history says first practice."""

        self.reminder.check()
        self.assertIn("first practice", self.reminder._balloon._message.text())
        self.reminder._balloon.hide()
        self._answer(self.now - 3 * 86400, count=3)
        self.reminder.check()
        self.assertIn("3 days", self.reminder._balloon._message.text())
        days = self.reminder._balloon._chart._days
        self.assertEqual(len(days), 7)
        self.assertEqual(sum(day.answers for day in days), 3)
        self.assertEqual(days[-1].answers, 0)

    def test_midnight_and_snooze_use_calendar_days_and_real_hours(self) -> None:
        """Keep a four-hour deadline across midnight; e.g. no reset to zero when the local date changes."""

        self.now = datetime(2026, 11, 12, 23).timestamp()
        self._answer(self.now - 86400)
        self.reminder.check()
        self.reminder.snooze()
        self.now += 3 * 3600
        self.reminder.check()
        self.assertFalse(self.reminder.visible)
        self.now += 3600
        self.reminder.check()
        self.assertTrue(self.reminder.visible)
        self.assertIn("2 days", self.reminder._balloon._message.text())

    def test_late_vocabulary_practice_stays_quiet_until_24_hours_even_after_restart(self) -> None:
        """Regress the 23:55-to-midnight reset; e.g. reuse the saved answer timestamp without inventing a new deadline."""

        last = datetime(2026, 11, 12, 23, 55).timestamp()
        self._answer(last)
        self.now = last + 5 * 60
        self.reminder.check()
        self.assertFalse(self.reminder.visible)
        self.reminder.stop()
        self.reminder = reminders.PracticeReminders(self.db, self.tray, lambda: True, clock=lambda: self.now)
        self.now = last + 24 * 3600 - 1
        self.reminder.check()
        self.assertFalse(self.reminder.visible)
        self.now += 1
        self.reminder.check()
        self.assertTrue(self.reminder.visible)
        self.assertEqual(self.db.setting("practice_snooze_until", "0"), "0")
        activity = practice_activity.read(self.db, now=datetime.fromtimestamp(self.now))
        self.assertEqual(activity.last_day, datetime.fromtimestamp(last).date())
        self.assertEqual(activity.week[-1].answers, 0)

    def test_late_verb_submission_has_the_same_minimum_interval(self) -> None:
        """Share spacing across practice modes; e.g. a pending typed correction still prevents a midnight reminder."""

        last = datetime(2026, 11, 12, 23, 55).timestamp()
        engine = verb_service.VerbService(self.db, verb_fixtures.FakeVerbProvider())
        opts = verb_fixtures.options(1).model_copy(update={"source": "starter"})
        with mock.patch("yt_whisper_subs.verb_db.time.time", return_value=last):
            session = engine.start(opts, lambda _: None)
            row = self.db.verb_current(session)
            self.db.verb_submit(row["id"], row["content"]["answer"], 1500)
        self.now = last + 5 * 60
        self.reminder.check()
        self.assertFalse(self.reminder.visible)
        self.now = last + 24 * 3600
        self.reminder.check()
        self.assertTrue(self.reminder.visible)

    def test_same_local_day_stays_credited_when_longer_than_24_hours(self) -> None:
        """Preserve daily credit alongside minimum spacing; e.g. a repeated clock hour can lengthen a local day."""

        first = datetime(2026, 10, 25, 0, 15, tzinfo=timezone(timedelta(hours=2)))
        later = datetime(2026, 10, 25, 23, 45, tzinfo=timezone(timedelta(hours=1)))
        self._answer(first.timestamp())
        self.now = later.timestamp()
        local_dates = {first.timestamp(): first, later.timestamp(): later}
        self.assertGreater(self.now - first.timestamp(), 24 * 3600)
        with mock.patch.object(reminders, "datetime") as local_time:
            local_time.fromtimestamp.side_effect = local_dates.__getitem__
            self.reminder.check()
        self.assertFalse(self.reminder.visible)

    def test_opening_practice_or_activity_is_not_completion(self) -> None:
        """Opening either page only snoozes; e.g. closing without answering still needs a later reminder."""

        opened = []
        self.reminder.requested.connect(opened.append)
        self.reminder.check()
        self.reminder._balloon.findChild(QtWidgets.QPushButton, "openPracticeActivity").click()
        self.assertEqual(opened, [True])
        self.assertFalse(self.reminder.visible)
        self.assertIsNone(self.db.practice_last_answer(self.now))
        self.now += 4 * 3600
        self.available = False
        self.reminder.check()
        self.assertFalse(self.reminder.visible)
        self.available = True
        self.reminder.check()
        self.assertTrue(self.reminder.visible)

    def test_disable_and_empty_vocabulary_stay_quiet(self) -> None:
        """Honor reminder settings independently of video notifications; e.g. disable without creating a snooze."""

        self.reminder.check()
        self.reminder.configure(reminders.ReminderPrefs(enabled=False))
        self.assertFalse(self.reminder.visible)
        self.assertEqual(self.db.setting("practice_snooze_until", "0"), "0")
        self.reminder.configure(reminders.ReminderPrefs())
        self.assertTrue(self.reminder.visible)
        self.reminder._balloon.hide()
        with mock.patch.object(self.db, "practice_has_candidates", return_value=False):
            self.reminder.check()
        self.assertFalse(self.reminder.visible)

    def test_background_practice_window_does_not_silence_daily_reminders(self) -> None:
        """Suppress only active practice, not a forgotten background window; e.g. a page left open for days."""

        self.reminder.stop()
        window = mock.Mock()
        window.isVisible.return_value = True
        window.isActiveWindow.return_value = True
        notifier = library_notifications.TrayNotifications(self.db, self.tray, practice_window=lambda: window)
        self.addCleanup(notifier.stop)
        self.reminder = notifier.practice
        self.reminder._clock = lambda: self.now
        self.reminder.check()
        self.assertFalse(self.reminder.visible)
        window.isActiveWindow.return_value = False
        self.reminder.check()
        self.assertTrue(self.reminder.visible)

    def test_balloon_slot_preserves_video_batch_and_aggregation_delay(self) -> None:
        """Queue rather than overlap balloons; e.g. snoozing practice reveals a waiting pinned-channel batch."""

        self.reminder.stop()
        notifier = library_notifications.TrayNotifications(self.db, self.tray)
        self.addCleanup(notifier.stop)
        self.reminder = notifier.practice
        self.reminder._clock = lambda: self.now
        self.reminder.check()
        self.assertTrue(self.reminder.visible)
        channel = self.db.add_channel("https://youtube.com/@alpha", "Alpha", False)
        meta = notification_fixtures._meta("aaaaaaaaaaa", "Alpha")
        self.db.store_snapshot(channel.channel_id, library_types.ChannelSnapshot(channel.url, "UC-Alpha", "Alpha", [meta], True))
        self.db.set_channel_pinned(channel.channel_id, True)
        notifier.configure(library_notifications.NotificationPrefs(delay_seconds=30, duration_seconds=0))
        notifier.add([meta])
        self.reminder.snooze()
        self.assertTrue(notifier._timer.isActive())
        self.assertFalse(notifier._balloon.isVisible())
        notifier._show_pending()
        self.assertTrue(notifier._balloon.isVisible())
        self.now += 4 * 3600
        self.reminder.check()
        self.assertFalse(self.reminder.visible)
        notifier._balloon.hide()
        self.app.processEvents()
        self.assertTrue(self.reminder.visible)

    def test_video_alert_temporarily_preempts_without_snoozing_practice(self) -> None:
        """Preserve existing video delivery while a persistent reminder is visible; e.g. restore practice after reading uploads."""

        self.reminder.stop()
        notifier = library_notifications.TrayNotifications(self.db, self.tray)
        self.addCleanup(notifier.stop)
        self.reminder = notifier.practice
        self.reminder._clock = lambda: self.now
        self.reminder.check()
        self.assertTrue(self.reminder.visible)
        channel = self.db.add_channel("https://youtube.com/@alpha", "Alpha", False)
        meta = notification_fixtures._meta("aaaaaaaaaaa", "Alpha")
        self.db.store_snapshot(channel.channel_id, library_types.ChannelSnapshot(channel.url, "UC-Alpha", "Alpha", [meta], True))
        self.db.set_channel_pinned(channel.channel_id, True)
        notifier.configure(library_notifications.NotificationPrefs(delay_seconds=30, duration_seconds=0))
        notifier.add([meta])
        self.assertTrue(self.reminder.visible)
        notifier._show_pending()
        self.app.processEvents()
        self.assertFalse(self.reminder.visible)
        self.assertTrue(notifier._balloon.isVisible())
        self.assertEqual(self.db.setting("practice_snooze_until", "0"), "0")
        notifier._balloon.hide()
        self.app.processEvents()
        self.assertTrue(self.reminder.visible)

    def test_activity_calendar_navigation_and_reported_effort(self) -> None:
        """Inspect actual saved day counts through native navigation; e.g. an inactive selected day reads zero."""

        self._answer(self.now - 2 * 86400, count=3)
        self._answer(self.now - 86400, count=2, direction=practice.Direction.EN_NL)
        panel = library_practice_activity.ActivityPanel(self.db)
        self.addCleanup(panel.close)
        panel.setAttribute(QtCore.Qt.WidgetAttribute.WA_DontShowOnScreen)
        panel.setStyleSheet(library_theme.STYLE)
        panel.resize(1000, 720)
        with mock.patch.object(practice_activity, "read", wraps=practice_activity.read) as read:
            panel.refresh()
        self.assertEqual(read.call_count, 1)
        panel.show()
        self.app.processEvents()
        selected = datetime.fromtimestamp(self.now - 2 * 86400).date()
        panel._calendar.setSelectedDate(QtCore.QDate(selected.year, selected.month, selected.day))
        self.assertIn("3 answers", panel._details.text())
        panel._calendar.showPreviousMonth()
        self.assertIn("0 answers", panel._month.text())
        panel._select_today()
        self.assertEqual(panel._calendar.selectedDate().day(), date.today().day)
        self.assertIn("No answers", panel._details.text())
        self.assertEqual(len(panel._chart._days), 7)


if __name__ == "__main__":
    unittest.main()
