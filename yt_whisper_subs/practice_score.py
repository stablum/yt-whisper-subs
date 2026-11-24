"""Summarize graded practice across modes without weighting by round size."""

from datetime import date, datetime

import polars as pl
import pydantic as pd

from yt_whisper_subs import practice_tracks
from yt_whisper_subs import sentence_score


class ModeScore(pd.BaseModel):
    """Expose the mean credit and sample size for one mode, e.g. seven dictations."""

    track: practice_tracks.Track
    score: float
    answers: int


class DayScore(pd.BaseModel):
    """Keep the daily score and its evidence together, e.g. 75% across two modes."""

    day: date
    score: float
    answers: int
    modes: list[ModeScore]


def _credit(row: dict) -> float:
    """Use each mode's own credit, e.g. a nearly correct dictation can score 0.9."""

    if row["outcome"] == "correct":
        return 1.0
    if row["partial"] is not None:
        return row["partial"]
    if row["corrected"] is not None:
        return sentence_score.partial(row["answer"], row["corrected"])
    return 0.0


def daily(db, *, now: datetime | None = None) -> list[DayScore]:
    """Average each practiced mode equally by local submission date, e.g. a 30-answer quiz and one dictation."""

    now = now or datetime.now()
    rows = db.practice_score_rows(now.timestamp())
    if not rows:
        return []
    answers = [
        {"day": datetime.fromtimestamp(row["answered_at"], now.tzinfo).date(),
         "track": row["track"], "credit": _credit(row)}
        for row in rows
    ]
    # Day score = mean of the mode means, each on the same 0–1 scale.
    by_mode = pl.DataFrame(answers).group_by("day", "track").agg(
        pl.col("credit").mean().alias("score"), pl.len().alias("answers"),
    ).sort("track")
    days = by_mode.group_by("day").agg(
        pl.col("score").mean(), pl.col("answers").sum(),
        pl.struct("track", "score", "answers").alias("modes"),
    ).sort("day")
    return [DayScore(**row) for row in days.to_dicts()]
