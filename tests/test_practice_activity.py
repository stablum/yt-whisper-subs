"""Verify activity against real answer transactions rather than mutable counters.

Example: `python -m unittest tests.test_practice_activity` uses no API calls.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests import listening_fixtures
from tests import practice_fixtures
from tests import sentence_fixtures
from yt_whisper_subs import library_db
from yt_whisper_subs import practice
from yt_whisper_subs import practice_activity
from yt_whisper_subs import practice_score
from yt_whisper_subs import practice_service
from yt_whisper_subs import practice_stats
from yt_whisper_subs import practice_tracks
from yt_whisper_subs import sentence_practice
from yt_whisper_subs import sentence_service


class ActivityTests(unittest.TestCase):
    """Exercise submitted choices, local dates and quarantine; e.g. reporting must not erase effort."""

    def setUp(self) -> None:
        """Create an isolated relational catalog and offline question strategy; e.g. count a real wrong answer."""

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = library_db.LibraryDb(Path(tmp.name) / "catalog.sqlite3")
        self.db.initialize()
        practice_fixtures.seed(self.db)
        self.engine = practice_service.PracticeService(self.db, practice_fixtures.FakeGenerator())
        self.now = datetime(2026, 11, 13, 12)

    def _round(self, at: datetime, count: int = 1, direction=practice.Direction.NL_EN) -> list[dict]:
        """Submit a complete real round at a controlled instant; e.g. create three answers on Wednesday."""

        opts = practice.RoundOptions(count=count, direction=direction, include_early=True)
        rows = []
        with mock.patch("yt_whisper_subs.practice_db.time.time", return_value=at.timestamp()):
            session = self.engine.start(opts, lambda _: None)
            while row := self.db.practice_current(session):
                choice = row["correct_idx"] if len(rows) % 2 else (row["correct_idx"] + 1) % 3
                rows.append(self.db.practice_answer(row["id"], choice, False, 1500))
        return rows

    def test_planned_displayed_and_reported_unanswered_are_not_activity(self) -> None:
        """Opening or reporting a question adds no effort credit; e.g. flag an unclear first question before answering."""

        session = self.engine.start(practice.RoundOptions(count=2, direction=practice.Direction.NL_EN), lambda _: None)
        row = self.db.practice_current(session)
        self.db.practice_flag(row["id"])
        data = practice_activity.read(self.db, now=self.now + timedelta(days=365))
        self.assertEqual(data.answers, 0)
        self.assertIsNone(data.last_day)
        self.assertIsNone(self.db.practice_last_answer((self.now + timedelta(days=365)).timestamp()))
        self.assertEqual([day.answers for day in data.week], [0] * 7)

    def test_both_directions_wrong_answers_and_later_reports_keep_effort(self) -> None:
        """Count work separately from correctness; e.g. quarantining an answered question retains its activity."""

        rows = self._round(self.now - timedelta(days=2), 2)
        self._round(self.now - timedelta(hours=1), direction=practice.Direction.EN_NL)
        self.db.practice_answer(rows[0]["id"], rows[0]["correct_idx"], False, 99)
        self.db.practice_flag(rows[0]["id"])
        data = practice_activity.read(self.db, now=self.now)
        self.assertEqual(data.answers, 3)
        self.assertEqual(sum(day.nl_en for day in data.days), 2)
        self.assertEqual(sum(day.en_nl for day in data.days), 1)
        self.assertEqual(sum(day.graded for day in data.days), 2)
        self.assertEqual(data.last_day, self.now.date())
        self.assertEqual(len(data.week), 7)
        self.assertEqual(data.week[0].day, self.now.date() - timedelta(days=6))
        # Keep both activity snapshots on the same local date, including when tests run after midnight.
        with mock.patch("yt_whisper_subs.practice_activity.datetime", wraps=datetime) as clock:
            clock.now.return_value = self.now
            stats = practice_stats.snapshot(self.db)
        self.assertEqual(stats["activity"].answers, data.answers)
        self.assertEqual(stats["streak"], data.streaks[0])

    def test_answer_date_controls_cross_midnight_activity(self) -> None:
        """Attribute an answer to its submission day; e.g. display at 23:59 and answer at 00:01."""

        before = self.now.replace(hour=23, minute=59) - timedelta(days=1)
        after = before + timedelta(minutes=2)
        with mock.patch("yt_whisper_subs.practice_db.time.time", return_value=before.timestamp()):
            session = self.engine.start(practice.RoundOptions(count=1, direction=practice.Direction.NL_EN), lambda _: None)
            row = self.db.practice_current(session)
        with mock.patch("yt_whisper_subs.practice_db.time.time", return_value=after.timestamp()):
            self.db.practice_answer(row["id"], row["correct_idx"], False, 120_000)
        data = practice_activity.read(self.db, now=self.now)
        self.assertEqual([(day.day, day.answers) for day in data.days], [(after.date(), 1)])

    def test_daily_score_balances_modes_and_uses_the_original_submission_day(self) -> None:
        """Weight a four-answer quiz and a one-answer dictation equally, even when grading happens later."""

        at = self.now - timedelta(days=2)
        self._round(at, 4)
        self._round(self.now - timedelta(hours=1), direction=practice.Direction.EN_NL)
        sentence_fixtures.seed(self.db)
        engine = sentence_service.SentenceService(
            self.db, sentence_fixtures.SimpleSentenceProvider(), speech=listening_fixtures.Speech(),
        )
        opts = sentence_fixtures.options(direction=sentence_practice.Direction.LISTEN)
        with mock.patch("yt_whisper_subs.sentence_db.time.time", return_value=at.timestamp()):
            session = engine.start(opts, lambda _: None)
            row = self.db.sentence_current(session)
            words = row["content"]["dutch"].split()
            self.db.sentence_submit(row["id"], " ".join(words[:-1]), 1000)
        pending = practice_score.daily(self.db, now=self.now)
        self.assertEqual([(d.day, d.score, d.answers) for d in pending], [(at.date(), .5, 4), (self.now.date(), 0, 1)])

        engine.check(row["id"], lambda _: None)
        day, later = practice_score.daily(self.db, now=self.now)
        partial = (len(words) - 1) / len(words)
        self.assertEqual((day.day, day.answers), (at.date(), 5))
        self.assertAlmostEqual(day.score, (.5 + partial) / 2)
        self.assertEqual({m.track: m.answers for m in day.modes},
                         {practice_tracks.Track.WORD_NL_EN: 4, practice_tracks.Track.LISTENING: 1})
        self.assertEqual(later, pending[-1])

        self.db.sentence_flag(row["id"])
        self.assertEqual(practice_score.daily(self.db, now=self.now), pending)
        self.assertEqual(practice_activity.read(self.db, now=self.now).answers, 6)

    def test_rolling_week_zero_fills_and_keeps_older_last_practice(self) -> None:
        """A quiet week still reports the real inactivity gap; e.g. the last answer was nine calendar days ago."""

        self._round(self.now - timedelta(days=9))
        data = practice_activity.read(self.db, now=self.now, since=self.now.date() - timedelta(days=6))
        self.assertEqual(data.answers, 0)
        self.assertEqual(data.gap, 9)
        self.assertTrue(all(day.answers == 0 for day in data.week))

    def test_streaks_follow_local_dates_and_not_hours_or_rounds(self) -> None:
        """Multiple rounds cannot inflate active days; e.g. preserve yesterday's streak until tonight."""

        for offset in (8, 7, 6, 2, 1, 1):
            self._round(self.now - timedelta(days=offset))
        data = practice_activity.read(self.db, now=self.now)
        self.assertEqual(data.streaks, (2, 3))
        self.assertEqual(len(data.days), 5)
        self.assertEqual(data.answers, 6)
        tz = timezone(timedelta(hours=1))
        previous = datetime(2026, 11, 12, 23, 59, tzinfo=tz)
        rows = [{"selected_idx": 0, "answered_at": previous.timestamp(), "outcome": "wrong", "word_id": 1, "direction": "nl_en"}]
        data = practice_activity.from_rows(rows, now=previous + timedelta(minutes=2))
        self.assertEqual(data.gap, 1)
        self.assertEqual(data.streaks, (1, 1))

    def test_typed_and_choice_answers_share_days_without_colliding_word_ids(self) -> None:
        """Keep separate item namespaces in the shared calendar; e.g. verb ID 1 is not vocabulary word ID 1."""

        rows = [
            {"selected_idx": 0, "answered_at": self.now.timestamp(), "outcome": "correct", "word_id": 1, "direction": "nl_en"},
            {"answer_text": "Jij werkt.", "answered_at": self.now.timestamp(), "outcome": None, "word_id": 1, "direction": "verbs"},
        ]
        data = practice_activity.from_rows(rows, now=self.now)
        self.assertEqual((data.answers, data.distinct_words, data.distinct_verbs), (2, 1, 1))
        self.assertEqual((data.week[-1].verbs, data.week[-1].graded), (1, 1))


if __name__ == "__main__":
    unittest.main()
