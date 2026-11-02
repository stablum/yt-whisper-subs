"""Polars learning metrics derived from immutable presentation and answer history.

Example: `snapshot(db)` supplies direction-specific daily accuracy and cumulative word coverage.
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta

import polars as pl


def _aggregate(frame: pl.DataFrame, keys: list[str]) -> pl.DataFrame:
    """Share denominators and timing policy across views; e.g. invalid questions never count as failures."""

    result = frame.group_by(keys).agg(
        pl.len().alias("shown"), pl.col("correct").sum(), pl.col("wrong").sum(), pl.col("invalid").sum(),
        pl.col("elapsed_ms").filter(pl.col("graded")).median().alias("median_ms"),
    ).with_columns((pl.col("correct") + pl.col("wrong")).alias("attempts"))
    return result.with_columns(
        pl.when(pl.col("attempts") > 0).then(pl.col("correct") / pl.col("attempts")).otherwise(None).alias("accuracy"))


def _history(db) -> tuple[list[dict], list[dict], list[dict]]:
    """Read all related histories in one SQLite snapshot; e.g. a background answer cannot split totals."""

    with db._connect() as conn:
        conn.execute("BEGIN")
        rows = [dict(row) for row in conn.execute(
            "SELECT p.id,p.shown_at,p.answered_at,p.elapsed_ms,p.outcome,s.direction,l.word_id,"
            "json_extract(q.body_json,'$.surface') AS word "
            "FROM practice_presentations p JOIN practice_sessions s ON s.id=p.session_id "
            "JOIN practice_questions q ON q.id=p.question_id JOIN vocab_lookups l ON l.id=q.lookup_id "
            "WHERE p.shown_at IS NOT NULL ORDER BY p.shown_at,p.id")]
        states = [dict(row) for row in conn.execute("SELECT * FROM practice_states")]
        rounds = [dict(row) for row in conn.execute(
            "SELECT s.id,s.created_at,s.direction,s.status,s.requested_count,COUNT(p.id) AS planned,"
            "COUNT(p.shown_at) AS shown,COALESCE(SUM(p.outcome='correct'),0) AS correct,"
            "COALESCE(SUM(p.outcome='wrong'),0) AS wrong,"
            "COALESCE(SUM(CASE WHEN p.outcome IN ('correct','wrong') THEN p.elapsed_ms ELSE 0 END),0) AS answer_ms "
            "FROM practice_sessions s LEFT JOIN practice_presentations p ON p.session_id=s.id "
            "GROUP BY s.id ORDER BY s.created_at DESC")]
    return rows, states, rounds


def snapshot(db) -> dict:
    """Aggregate real presentations with explicit grading rules; e.g. compare both learning directions."""

    rows, states, rounds = _history(db)
    empty = {"shown": 0, "correct": 0, "wrong": 0, "invalid": 0, "attempts": 0, "accuracy": None, "median_ms": None}
    if not rows:
        return {"daily": [], "coverage": [], "terms": [], "rounds": rounds,
                "totals": empty, "due": 0, "established": 0, "streak": 0}
    for row in rows:
        row["shown_day"] = datetime.fromtimestamp(row["shown_at"]).date().isoformat()
        instant = row["answered_at"] if row["outcome"] in {"correct", "wrong"} else row["shown_at"]
        row["day"] = datetime.fromtimestamp(instant).date().isoformat()
    frame = pl.DataFrame(rows, infer_schema_length=None, schema_overrides={"elapsed_ms": pl.Float64}).with_columns(
        (pl.col("outcome") == "correct").fill_null(False).alias("correct"),
        (pl.col("outcome") == "wrong").fill_null(False).alias("wrong"),
        (pl.col("outcome") == "invalid").fill_null(False).alias("invalid"),
        pl.col("outcome").is_in(["correct", "wrong"]).fill_null(False).alias("graded"),
        pl.lit("all").alias("all"),
    )
    daily = _aggregate(frame, ["day", "direction"]).sort("day", "direction").to_dicts()
    names = frame.group_by("word_id", "direction").agg(pl.col("word").last())
    terms = _aggregate(frame, ["word_id", "direction"]).join(names, on=["word_id", "direction"]).to_dicts()
    state_map = {(row["word_id"], row["direction"]): row for row in states}
    latest = frame.filter(pl.col("graded")).group_by("word_id", "direction").agg(pl.col("outcome").last())
    last_outcome = {(row["word_id"], row["direction"]): row["outcome"] for row in latest.to_dicts()}
    established = 0
    for term in terms:
        key = (term["word_id"], term["direction"])
        state = state_map.get(key)
        card = json.loads(state["card_json"]) if state else {}
        term["due_at"] = state["due_at"] if state else None
        term["stability"] = card.get("stability") or 0
        established += term["correct"] >= 3 and last_outcome.get(key) == "correct" and term["stability"] >= 21
    coverage = frame.group_by("word_id").agg(pl.col("shown_day").min().alias("day")).group_by("day").len().sort("day")
    coverage = coverage.with_columns(pl.col("len").cum_sum().alias("words"), pl.col("day").str.to_date())
    first, last = coverage["day"].min(), date.fromisoformat(max(frame["day"].max(), frame["shown_day"].max()))
    calendar = pl.DataFrame({"day": pl.date_range(first, last, eager=True)})
    coverage = calendar.join(coverage, on="day", how="left").with_columns(pl.col("words").forward_fill())
    coverage = coverage.select(pl.col("day").dt.to_string("%Y-%m-%d"), "words").to_dicts()
    graded = frame.filter(pl.col("graded"))
    active_dates = {date.fromisoformat(day) for day in graded["day"].unique().to_list()}
    cursor = date.today() if date.today() in active_dates else date.today() - timedelta(days=1)
    streak = 0
    while cursor in active_dates:
        streak += 1
        cursor -= timedelta(days=1)
    return {"daily": daily, "coverage": coverage, "terms": terms, "rounds": rounds,
            "totals": _aggregate(frame, ["all"]).drop("all").row(0, named=True),
            "due": sum(row["due_at"] <= time.time() for row in states), "established": established, "streak": streak}
