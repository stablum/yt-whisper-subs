"""Shared local-day activity accounting for reminders, the calendar and progress.

Example: `read(db).week` includes zero-answer days and counts attempts even after a question is reported.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta

import polars as pl
import pydantic as pd


class Day(pd.BaseModel):
    """Describe effort separately from grading; e.g. a wrong answer still contributes one answer."""

    day: date
    answers: int = 0
    correct: int = 0
    graded: int = 0
    words: int = 0
    nl_en: int = 0
    en_nl: int = 0
    verbs: int = 0
    verb_lemmas: int = 0
    sentences: int = 0
    sentence_nl_en: int = 0
    sentence_en_nl: int = 0
    listening: int = 0
    comprehension_read: int = 0
    comprehension_listen: int = 0


class Activity(pd.BaseModel):
    """Carry one consistent activity snapshot; e.g. reuse the same seven bars in a balloon and activity page."""

    today: date
    days: list[Day] = pd.Field(default_factory=list)
    last_day: date | None = None
    distinct_words: int = 0
    distinct_verbs: int = 0

    @property
    def week(self) -> list[Day]:
        """Return exactly seven local calendar days ending today; e.g. Thursday through Wednesday."""

        saved = {row.day: row for row in self.days}
        dates = [self.today - timedelta(days=offset) for offset in range(6, -1, -1)]
        return [saved.get(day, Day(day=day)) for day in dates]

    @property
    def answers(self) -> int:
        """Count submitted answers rather than visits or planned questions; e.g. opening a round adds zero."""

        return sum(row.answers for row in self.days)

    @property
    def gap(self) -> int | None:
        """Measure the gap in local dates, not 24-hour blocks; e.g. yesterday is one day ago across DST."""

        return (self.today - self.last_day).days if self.last_day else None

    @property
    def streaks(self) -> tuple[int, int]:
        """Return current and longest consecutive study days; e.g. yesterday's streak stays live until tonight."""

        active = sorted(row.day for row in self.days if row.answers)
        run = longest = 0
        prev = None
        for day in active:
            run = run + 1 if prev and day == prev + timedelta(days=1) else 1
            longest, prev = max(longest, run), day
        current = run if prev and prev >= self.today - timedelta(days=1) else 0
        return current, longest


def from_rows(rows: list[dict], *, now: datetime | None = None) -> Activity:
    """Aggregate immutable submitted choices with Polars; e.g. a report before answering contributes nothing."""

    now = now or datetime.now()
    answers = []
    for row in rows:
        stamp = row.get("answered_at")
        submitted = row.get("selected_idx") is not None or row.get("answer_text") is not None
        if not submitted or stamp is None or stamp > now.timestamp():
            continue
        word_ids = json.loads(row["word_ids_json"]) if row.get("word_ids_json") else [row["word_id"]]
        if row["direction"] == "verbs":
            word_ids = []
        answers.append({
            "day": datetime.fromtimestamp(stamp, now.tzinfo).date(), "word_id": row["word_id"],
            "word_ids": word_ids,
            "correct": row["outcome"] == "correct", "graded": row["outcome"] in {"correct", "wrong"},
            "nl_en": row["direction"] == "nl_en", "en_nl": row["direction"] == "en_nl",
            "verbs": row["direction"] == "verbs",
            "sentences": row["direction"] in {"sentence_en_nl", "sentence_nl_en"},
            "listening": row["direction"] == "sentence_listen_nl",
            'comprehension_read': row['direction'] == 'comprehension_read',
            'comprehension_listen': row['direction'] == 'comprehension_listen',
            "sentence_nl_en": row["direction"] == "sentence_nl_en", "sentence_en_nl": row["direction"] == "sentence_en_nl",
        })
    if not answers:
        return Activity(today=now.date())
    frame = pl.DataFrame(answers, schema_overrides={"word_ids": pl.List(pl.Int64)})
    days = frame.group_by("day").agg(
        pl.len().alias("answers"), pl.col("correct").sum(), pl.col("graded").sum(),
        pl.col("word_ids").explode(empty_as_null=True).drop_nulls().n_unique().alias("words"),
        pl.col("word_id").filter(pl.col("verbs")).n_unique().alias("verb_lemmas"),
        pl.col("nl_en").sum(), pl.col("en_nl").sum(), pl.col("verbs").sum(),
        pl.col("sentences").sum(), pl.col("sentence_nl_en").sum(), pl.col("sentence_en_nl").sum(),
        pl.col("listening").sum(),
        pl.col('comprehension_read').sum(), pl.col('comprehension_listen').sum(),
    ).sort("day")
    return Activity(today=now.date(), last_day=frame["day"].max(),
                    distinct_words=frame["word_ids"].explode(empty_as_null=True).drop_nulls().n_unique(),
                    distinct_verbs=frame.filter(pl.col("verbs"))["word_id"].n_unique(),
                    days=[Day(**row) for row in days.to_dicts()])


def read(db, *, now: datetime | None = None, since: date | None = None) -> Activity:
    """Use indexed events for either a full calendar or a rolling week; e.g. reminder checks stay small."""

    now = now or datetime.now()
    start = datetime.combine(since, time.min, now.tzinfo).timestamp() if since else 0
    result = from_rows(db.practice_activity_rows(start, now.timestamp()), now=now)
    last = db.practice_last_answer(now.timestamp())
    result.last_day = datetime.fromtimestamp(last, now.tzinfo).date() if last is not None else None
    return result
