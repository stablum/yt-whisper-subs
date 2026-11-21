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
from tests import sentence_fixtures
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
from yt_whisper_subs import sentence_service


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
        self._start_notifier()
        self.addCleanup(self._cleanup)

    def _start_notifier(self) -> None:
        """Recreate the coordinator with the same catalog and clock, as on tray restart."""

        self.notifier = library_notifications.TrayNotifications(
            self.db, self.tray, practicing=lambda: not self.available,
        )
        self.notifier._practice._clock = lambda: self.now
        self.balloon = self.notifier._balloon

    @property
    def _practice_visible(self) -> bool:
        """Check whether practice content is actually visible in the shared balloon."""

        return self.balloon._practice is not None and self.balloon._practice.isVisible()

    def _cleanup(self) -> None:
        """Stop timers before removing the catalog; e.g. a queued startup check cannot touch a deleted database."""

        self.notifier.stop()
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

        self.notifier.check_practice()
        self.assertTrue(self._practice_visible)
        self.balloon.findChild(QtWidgets.QPushButton, "snoozePractice").click()
        deadline = float(self.db.setting("practice_snooze_until", "0"))
        self.assertEqual(deadline, self.now + 4 * 3600)
        self.notifier.stop()
        self._start_notifier()
        self.now = deadline - 1
        self.notifier.check_practice()
        self.assertFalse(self._practice_visible)
        self.now = deadline
        self.notifier.check_practice()
        self.assertTrue(self._practice_visible)
        self._answer(self.now)
        self.notifier.check_practice()
        self.assertFalse(self._practice_visible)
        self.now += 2 * 3600
        self.notifier.check_practice()
        self.assertFalse(self._practice_visible)

    def test_verb_only_history_enables_reminders_and_submitted_verbs_complete_today(self) -> None:
        """Share daily habit accounting across modes; e.g. a saved typed attempt suppresses reminders even if grading is pending."""

        catalog = verb_fixtures.catalog(self.db)
        engine = verb_service.VerbService(self.db, verb_fixtures.FakeVerbProvider())
        with mock.patch.object(self.db, "practice_has_candidates", return_value=False):
            self.notifier.check_practice()
            self.assertFalse(self._practice_visible)
            with mock.patch.object(self.db, "verb_catalog", return_value=catalog):
                session = engine.start(verb_fixtures.options(1), lambda _: None)
            self.notifier.check_practice()
            self.assertTrue(self._practice_visible)
            with mock.patch("yt_whisper_subs.verb_db.time.time", return_value=self.now):
                row = self.db.verb_current(session)
                self.db.verb_submit(row["id"], row["content"]["answer"], 1500)
            self.notifier.check_practice()
            self.assertFalse(self._practice_visible)
            self.now += 2 * 86400
            self.notifier.check_practice()
            self.assertTrue(self._practice_visible)
            self.assertIn("2 days", self.balloon._practice._message.text())

    def test_close_escape_and_stop_have_distinct_persistence(self) -> None:
        """User dismissal snoozes but application shutdown does not; e.g. close using the window manager."""

        self.notifier.configure_practice(reminders.ReminderPrefs(snooze_hours=6))
        QtTest.QTest.keyClick(self.balloon, QtCore.Qt.Key.Key_Escape)
        self.assertEqual(float(self.db.setting("practice_snooze_until", "0")), self.now + 6 * 3600)
        self.now += 6 * 3600
        self.notifier.check_practice()
        self.balloon.close()
        deadline = self.db.setting("practice_snooze_until", "0")
        self.assertEqual(float(deadline), self.now + 6 * 3600)
        self.now += 6 * 3600
        self.notifier.check_practice()
        self.notifier.stop()
        self.assertEqual(self.db.setting("practice_snooze_until", "0"), deadline)

    def test_gap_and_seven_zero_days_and_never_practiced(self) -> None:
        """Render known gaps without inventing a start date; e.g. an empty vocabulary history says first practice."""

        self.notifier.check_practice()
        self.assertIn("first practice", self.balloon._practice._message.text())
        self._answer(self.now - 3 * 86400, count=3)
        self.notifier.check_practice()
        self.assertIn("3 days", self.balloon._practice._message.text())
        days = self.balloon._practice._chart._days
        self.assertEqual(len(days), 7)
        self.assertEqual(sum(day.answers for day in days), 3)
        self.assertEqual(days[-1].answers, 0)

    def test_midnight_and_snooze_use_calendar_days_and_real_hours(self) -> None:
        """Keep a four-hour deadline across midnight; e.g. no reset to zero when the local date changes."""

        self.now = datetime(2026, 11, 12, 23).timestamp()
        self._answer(self.now - 86400)
        self.notifier.check_practice()
        self.notifier.snooze_practice()
        self.now += 3 * 3600
        self.notifier.check_practice()
        self.assertFalse(self._practice_visible)
        self.now += 3600
        self.notifier.check_practice()
        self.assertTrue(self._practice_visible)
        self.assertIn("2 days", self.balloon._practice._message.text())

    def test_late_vocabulary_practice_stays_quiet_until_24_hours_even_after_restart(self) -> None:
        """Regress the 23:55-to-midnight reset; e.g. reuse the saved answer timestamp without inventing a new deadline."""

        last = datetime(2026, 11, 12, 23, 55).timestamp()
        self._answer(last)
        self.now = last + 5 * 60
        self.notifier.check_practice()
        self.assertFalse(self._practice_visible)
        self.notifier.stop()
        self._start_notifier()
        self.now = last + 24 * 3600 - 1
        self.notifier.check_practice()
        self.assertFalse(self._practice_visible)
        self.now += 1
        self.notifier.check_practice()
        self.assertTrue(self._practice_visible)
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
        self.notifier.check_practice()
        self.assertFalse(self._practice_visible)
        self.now = last + 24 * 3600
        self.notifier.check_practice()
        self.assertTrue(self._practice_visible)

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
            self.notifier.check_practice()
        self.assertFalse(self._practice_visible)

    def test_late_sentence_submission_quiets_reminder_while_correction_is_pending(self) -> None:
        """Give full habit credit to a saved translation attempt; e.g. a 23:55 writing session cannot trigger a midnight reminder."""

        sentence_fixtures.seed(self.db)
        engine = sentence_service.SentenceService(self.db, sentence_fixtures.FakeSentenceProvider())
        last = datetime(2026, 11, 12, 23, 55).timestamp()
        # Vocabulary fixtures share the catalog: sentence generation still sees only its own approved target set.
        candidates = self.db.word_candidates("en_nl")
        words = [row for row in candidates if row["lemma"] in {"slim", "fiets", "lopen"}]
        with mock.patch.object(self.db, "word_candidates", return_value=words):
            session = engine.start(sentence_fixtures.options(), lambda _: None)
        with mock.patch("yt_whisper_subs.sentence_db.time.time", return_value=last):
            row = self.db.sentence_current(session)
            self.db.sentence_submit(row["id"], sentence_fixtures.DUTCH, 1500)
        self.now = last + 5 * 60
        self.notifier.check_practice()
        self.assertFalse(self._practice_visible)
        self.now = last + 24 * 3600 - 1
        self.notifier.check_practice()
        self.assertFalse(self._practice_visible)
        self.now += 1
        self.notifier.check_practice()
        self.assertTrue(self._practice_visible)

    def test_opening_practice_or_activity_is_not_completion(self) -> None:
        """Opening either page only snoozes; e.g. closing without answering still needs a later reminder."""

        opened = []
        self.notifier.practice_requested.connect(opened.append)
        self.notifier.check_practice()
        self.balloon.findChild(QtWidgets.QPushButton, "openPracticeActivity").click()
        self.assertEqual(opened, [True])
        self.assertFalse(self._practice_visible)
        self.assertIsNone(self.db.practice_last_answer(self.now))
        self.now += 4 * 3600
        self.available = False
        self.notifier.check_practice()
        self.assertFalse(self._practice_visible)
        self.available = True
        self.notifier.check_practice()
        self.assertTrue(self._practice_visible)
        start = self.balloon.findChild(QtWidgets.QPushButton, "primaryButton")
        start.click()
        self.assertEqual(opened, [True, False])
        self.assertFalse(self.balloon.isVisible())

    def test_disable_and_empty_vocabulary_stay_quiet(self) -> None:
        """Honor reminder settings independently of video notifications; e.g. disable without creating a snooze."""

        self.notifier.check_practice()
        self.notifier.configure_practice(reminders.ReminderPrefs(enabled=False))
        self.assertFalse(self._practice_visible)
        self.assertEqual(self.db.setting("practice_snooze_until", "0"), "0")
        self.notifier.configure_practice(reminders.ReminderPrefs())
        self.assertTrue(self._practice_visible)
        with mock.patch.object(self.db, "practice_has_candidates", return_value=False):
            self.notifier.check_practice()
        self.assertFalse(self._practice_visible)

    def test_background_practice_window_does_not_silence_daily_reminders(self) -> None:
        """Suppress only active practice, not a forgotten background window; e.g. a page left open for days."""

        self.available = False
        self.notifier.check_practice()
        self.assertFalse(self._practice_visible)
        self.available = True
        self.notifier.check_practice()
        self.assertTrue(self._practice_visible)

    def _queue_video(self) -> None:
        """Queue a pinned discovery with a grouping delay and a timed reading interval."""

        channel = self.db.add_channel("https://youtube.com/@alpha", "Alpha", False)
        meta = notification_fixtures._meta("aaaaaaaaaaa", "Alpha")
        snapshot = library_types.ChannelSnapshot(channel.url, "UC-Alpha", "Alpha", [meta], True)
        self.db.store_snapshot(channel.channel_id, snapshot)
        self.db.set_channel_pinned(channel.channel_id, True)
        self.notifier.configure(library_notifications.NotificationPrefs(delay_seconds=30, duration_seconds=20))
        self.notifier.add([meta])

    def test_video_batch_joins_open_reminder_and_snooze_keeps_videos(self) -> None:
        """Group the titles first, then let Snooze remove only the practice section."""

        self.notifier.check_practice()
        self.assertTrue(self._practice_visible)
        self._queue_video()
        self.assertTrue(self.notifier._timer.isActive())
        self.assertFalse(self.balloon._videos.isVisible())
        self.notifier._show_pending()
        self.assertTrue(self.balloon._videos.isVisible())
        self.assertTrue(self._practice_visible)
        self.assertIs(self.balloon._practice.window(), self.balloon)
        self.assertIn("News <aaaaaaaaaaa>", self.balloon._videos.toPlainText())
        self.assertEqual(self.db.setting("practice_snooze_until", "0"), "0")
        self.balloon.findChild(QtWidgets.QPushButton, "snoozePractice").click()
        self.assertFalse(self._practice_visible)
        self.assertTrue(self.balloon._videos.isVisible())
        self.assertEqual(float(self.db.setting("practice_snooze_until", "0")), self.now + 4 * 3600)
        self.now += 4 * 3600
        self.notifier.check_practice()
        self.assertTrue(self._practice_visible)
        self.balloon.findChild(QtWidgets.QToolButton, "dismissNotification").click()
        self.assertFalse(self.balloon.isVisible())
        self.assertFalse(self.notifier._pending)
        self.assertEqual(float(self.db.setting("practice_snooze_until", "0")), self.now + 4 * 3600)

    def test_waiting_reminder_shares_video_delivery_and_survives_video_timeout(self) -> None:
        """Present a queued batch with practice and expire only the timed titles."""

        self._queue_video()
        self.notifier.check_practice()
        self.assertFalse(self.balloon.isVisible())
        self.notifier._show_pending()
        self.assertTrue(self._practice_visible)
        self.assertTrue(self.balloon._videos.isVisible())
        self.balloon._timer.timeout.emit()
        self.assertTrue(self._practice_visible)
        self.assertFalse(self.balloon._videos.isVisible())
        self.assertEqual(self.db.setting("practice_snooze_until", "0"), "0")
        self.assertIn("Time for practice", self.balloon._heading.text())

    def test_dismiss_before_batch_is_ready_preserves_the_grouping_delay(self) -> None:
        """A practice-only dismissal keeps a pending video batch scheduled."""

        self.notifier.check_practice()
        self._queue_video()
        self.balloon.reject()
        self.assertFalse(self.balloon.isVisible())
        self.assertTrue(self.notifier._timer.isActive())
        self.notifier._show_pending()
        self.assertTrue(self.balloon._videos.isVisible())
        self.assertFalse(self._practice_visible)

    def test_video_settings_remove_only_the_video_section(self) -> None:
        """Disabling alerts preserves the due reminder and its existing snooze state."""

        self._queue_video()
        self.notifier._show_pending()
        self.notifier.configure(library_notifications.NotificationPrefs(enabled=False))
        self.assertTrue(self._practice_visible)
        self.assertFalse(self.balloon._videos.isVisible())
        self.assertEqual(self.db.setting("practice_snooze_until", "0"), "0")

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
