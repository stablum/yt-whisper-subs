"""Auditable verb skill and error analytics derived with Polars.

Example: tense accuracy uses actual tense assessments, rather than blaming every dimension for one typo.
"""

from __future__ import annotations

import time
from datetime import datetime

import polars as pl

from yt_whisper_subs import verb_db


_ASSESSMENTS = (
    "SELECT a.axis,a.correct,t.tense,t.subject,t.structure,CAST(t.verb_id AS TEXT) AS verb "
    "FROM verb_assessments a JOIN verb_presentations p ON p.id=a.presentation_id "
    "JOIN verb_questions q ON q.id=p.question_id JOIN verb_targets t ON t.id=q.target_id "
    "WHERE p.outcome IN ('correct','wrong') AND (a.correct=1 OR EXISTS ("
    "SELECT 1 FROM verb_errors e WHERE e.presentation_id=a.presentation_id AND e.axis=a.axis AND e.kind<>'meaning'))"
)


def metrics(db) -> list[dict]:
    """Aggregate independent dimensions for adaptive selection; e.g. exclude every use of a reported question."""

    with db._connect() as conn:
        rows = [dict(row) for row in conn.execute(_ASSESSMENTS)]
    return _metrics(rows)


def _metrics(rows: list[dict]) -> list[dict]:
    """Share dimension denominators between selection and charts; e.g. read the dashboard in one consistent snapshot."""

    if not rows:
        return []
    for row in rows:
        row["value"] = row[row["axis"]]
    frame = pl.DataFrame(rows).group_by("axis", "value").agg(
        pl.len().alias("attempts"), (1 - pl.col("correct")).sum().alias("failures"))
    return frame.with_columns((1 - pl.col("failures") / pl.col("attempts")).alias("accuracy")).to_dicts()


def snapshot(db) -> dict:
    """Read presentation, diagnosis and review evidence in one snapshot; e.g. pending answers count effort, not failure."""

    with db._connect() as conn:
        conn.execute("BEGIN")
        rows = [dict(row) for row in conn.execute(verb_db.PRESENTATION + " WHERE p.shown_at IS NOT NULL ORDER BY p.id")]
        errors = [dict(row) for row in conn.execute(
            "SELECT e.*,p.answer_text,p.answered_at,t.tense,t.subject,t.structure,v.lemma "
            "FROM verb_errors e JOIN verb_presentations p ON p.id=e.presentation_id "
            "JOIN verb_questions q ON q.id=p.question_id JOIN verb_targets t ON t.id=q.target_id "
            "JOIN verb_lemmas v ON v.id=t.verb_id WHERE p.outcome IN ('correct','wrong') ORDER BY p.answered_at DESC,e.ordinal")]
        due = conn.execute("SELECT COUNT(*) FROM verb_states WHERE due_at<=?", (time.time(),)).fetchone()[0]
        assessments = [dict(row) for row in conn.execute(_ASSESSMENTS)]
        rounds = [dict(row) for row in conn.execute(
            "SELECT s.id,s.created_at,s.status,COUNT(p.id) AS planned,COUNT(p.answer_text) AS submitted,"
            "COALESCE(SUM(p.outcome='correct'),0) AS correct,COALESCE(SUM(p.outcome='wrong'),0) AS wrong "
            "FROM verb_sessions s LEFT JOIN verb_presentations p ON p.session_id=s.id GROUP BY s.id ORDER BY s.created_at DESC")]
    graded = [row for row in rows if row["outcome"] in {"correct", "wrong"}]
    daily = []
    terms = []
    if graded:
        frame = pl.DataFrame([{"day": datetime.fromtimestamp(row["answered_at"]).date().isoformat(),
                               "correct": row["outcome"] == "correct", "lemma": row["lemma"],
                               "variant": row["variant"], "elapsed_ms": row["elapsed_ms"]} for row in graded])
        daily = frame.group_by("day", "variant").agg(pl.len().alias("attempts"), pl.col("correct").sum()).sort("day", "variant").to_dicts()
    if rows:
        frame = pl.DataFrame([{"lemma": row["lemma"], "submitted": row["answer_text"] is not None,
                               "graded": row["outcome"] in {"correct", "wrong"}, "correct": row["outcome"] == "correct",
                               "elapsed_ms": row["elapsed_ms"]} for row in rows], schema_overrides={"elapsed_ms": pl.Float64})
        terms = frame.group_by("lemma").agg(pl.len().alias("shown"), pl.col("submitted").sum(),
                                              pl.col("graded").sum().alias("attempts"), pl.col("correct").sum(),
                                              pl.col("elapsed_ms").filter(pl.col("graded")).median().alias("median_ms")).sort("lemma").to_dicts()
    kinds = []
    if errors:
        kinds = pl.DataFrame(errors).group_by("kind").agg(
            pl.col("presentation_id").n_unique().alias("answers"), pl.len().alias("errors")).sort("answers", descending=True).to_dicts()
    correct = sum(row["outcome"] == "correct" for row in graded)
    return {"shown": len(rows), "submitted": sum(row["answer_text"] is not None for row in rows),
            "graded": len(graded), "correct": correct, "wrong": len(graded) - correct, "due": due,
            "pending": sum(row["answer_text"] is not None and row["outcome"] is None for row in rows),
            "daily": daily, "terms": terms, "metrics": _metrics(assessments), "errors": errors, "kinds": kinds, "rounds": rounds}
