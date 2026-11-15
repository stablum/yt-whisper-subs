"""Per-direction vocabulary, inflection, context and full-sentence learning metrics.

Example: the same answer can earn word-meaning credit and still need a sentence-level word-order review.
"""

from __future__ import annotations

import time
from datetime import datetime

import polars as pl

from yt_whisper_subs import sentence_score


_FORMS = """
SELECT f.feature,f.correct,s.direction FROM sentence_forms f
JOIN sentence_presentations p ON p.id=f.presentation_id JOIN sentence_sessions s ON s.id=p.session_id
WHERE p.outcome IN ('correct','wrong')
"""
_ANSWERS = """
SELECT p.id,p.session_id,p.shown_at,p.answered_at,p.answer_text,p.elapsed_ms,p.outcome,q.direction,q.scene,
       json_extract(p.grade_json,'$.corrected') AS corrected
FROM sentence_presentations p JOIN sentence_questions q ON q.id=p.question_id
"""


def metrics(db, direction: str) -> dict:
    """Supply compact form/context difficulty to selection; e.g. no personal sentence history is sent for planning."""

    with db._connect() as conn:
        forms = [dict(row) for row in conn.execute(_FORMS + " AND s.direction=? AND f.correct IS NOT NULL", (direction,))]
        answers = [dict(row) for row in conn.execute(_ANSWERS + " WHERE p.outcome IN ('correct','wrong') AND q.direction=?", (direction,))]
        kinds = [dict(row) for row in conn.execute(
            "SELECT e.kind,COUNT(*) AS failures FROM sentence_errors e JOIN sentence_presentations p ON p.id=e.presentation_id "
            "JOIN sentence_questions q ON q.id=p.question_id WHERE p.outcome='wrong' AND q.direction=? GROUP BY e.kind "
            "ORDER BY failures DESC", (direction,))]
    form_rows = []
    if forms:
        form_rows = pl.DataFrame(forms).group_by("feature").agg(
            pl.len().alias("attempts"), (1 - pl.col("correct")).sum().alias("failures")).to_dicts()
    scenes = []
    if answers:
        scenes = pl.DataFrame(answers, infer_schema_length=None).group_by("scene").agg(
            pl.len().alias("attempts"), (pl.col("outcome") == "wrong").sum().alias("failures")).to_dicts()
    return {"forms": form_rows, "scenes": scenes, "kinds": [row["kind"] for row in kinds[:3]]}


def snapshot(db) -> dict:
    """Read a consistent relational snapshot, then aggregate with Polars; e.g. reported work keeps exposure but loses grading influence."""

    with db._connect() as conn:
        conn.execute("BEGIN")
        rows = [dict(row) for row in conn.execute(_ANSWERS + " WHERE p.shown_at IS NOT NULL ORDER BY p.id")]
        exposures = [dict(row) for row in conn.execute(
            "SELECT p.id,l.word_id,q.direction,json_extract(l.context_json,'$.word') AS word,l.meaning "
            "FROM sentence_presentations p JOIN sentence_questions q ON q.id=p.question_id "
            "JOIN sentence_question_words w ON w.question_id=q.id JOIN vocab_lookups l ON l.id=w.lookup_id WHERE p.shown_at IS NOT NULL")]
        assessments = [dict(row) for row in conn.execute(
            "SELECT p.id,l.word_id,q.direction,a.meaning_correct,a.form_correct,a.target_used FROM sentence_assessments a "
            "JOIN sentence_presentations p ON p.id=a.presentation_id JOIN sentence_questions q ON q.id=p.question_id "
            "JOIN vocab_lookups l ON l.id=a.lookup_id WHERE p.outcome IN ('correct','wrong')")]
        forms = [dict(row) for row in conn.execute(_FORMS)]
        errors = [dict(row) for row in conn.execute(
            "SELECT e.*,p.answer_text,p.answered_at,q.direction,q.scene,json_extract(p.grade_json,'$.corrected') AS corrected "
            "FROM sentence_errors e JOIN sentence_presentations p ON p.id=e.presentation_id "
            "JOIN sentence_questions q ON q.id=p.question_id WHERE p.outcome='wrong' ORDER BY p.answered_at DESC,e.ordinal")]
        states = [dict(row) for row in conn.execute("SELECT * FROM sentence_states")]
        due = conn.execute("SELECT COUNT(*) FROM sentence_question_states WHERE due_at<=?", (time.time(),)).fetchone()[0]
        rounds = [dict(row) for row in conn.execute(
            "SELECT s.id,s.direction,s.created_at,s.status,COUNT(p.id) AS planned,COUNT(p.answer_text) AS submitted,"
            "COALESCE(SUM(p.outcome='correct'),0) AS correct,COALESCE(SUM(p.outcome='wrong'),0) AS wrong "
            "FROM sentence_sessions s LEFT JOIN sentence_presentations p ON p.session_id=s.id GROUP BY s.id ORDER BY s.created_at DESC")]
    graded = [{**row, "partial_score": sentence_score.partial(row["answer_text"], row["corrected"])}
              for row in rows if row["outcome"] in {"correct", "wrong"}]
    daily, contexts, form_rows = [], [], []
    partial_score = None
    if graded:
        frame = pl.DataFrame([{"day": datetime.fromtimestamp(row["answered_at"]).date().isoformat(),
                               "session_id": row["session_id"], "direction": row["direction"], "scene": row["scene"],
                               "correct": row["outcome"] == "correct", "partial_score": row["partial_score"]}
                              for row in graded])
        summary = [pl.len().alias("attempts"), pl.col("correct").sum(), pl.col("partial_score").mean()]
        daily = frame.group_by("day", "direction").agg(summary).sort("day", "direction").to_dicts()
        contexts = frame.group_by("scene", "direction").agg(summary).to_dicts()
        partial_score = frame["partial_score"].mean()
        round_scores = dict(frame.group_by("session_id").agg(pl.col("partial_score").mean()).iter_rows())
        rounds = [{**row, "partial_score": round_scores.get(row["id"])} for row in rounds]
    else:
        rounds = [{**row, "partial_score": None} for row in rounds]
    if forms:
        form_rows = pl.DataFrame(forms, schema_overrides={"correct": pl.Int64}).group_by("feature", "direction").agg(
            pl.col("correct").count().alias("attempts"), (pl.col("correct") == 1).sum().alias("correct"),
            (pl.col("correct") == 0).sum().alias("failures")).to_dicts()
    terms = _terms(exposures, assessments, states)
    return {"shown": len(rows), "submitted": sum(row["answer_text"] is not None for row in rows),
            "graded": len(graded), "correct": sum(row["outcome"] == "correct" for row in graded),
            "wrong": sum(row["outcome"] == "wrong" for row in graded), "question_due": due,
            "word_due": sum(row["due_at"] <= time.time() for row in states),
            "pending": sum(row["answer_text"] is not None and row["outcome"] is None for row in rows),
            "partial_score": partial_score, "answers": graded,
            "daily": daily, "scenes": contexts, "forms": form_rows, "terms": terms, "errors": errors, "rounds": rounds}


def _terms(exposures: list[dict], assessments: list[dict], states: list[dict]) -> list[dict]:
    """Join true exposure counts to independent meaning/form outcomes; e.g. unassessable endings never inflate a failure denominator."""

    if not exposures:
        return []
    frame = pl.DataFrame(exposures).group_by("word_id", "direction").agg(
        pl.col("id").n_unique().alias("shown"), pl.col("word").last(), pl.col("meaning").last())
    values = {}
    if assessments:
        grades = pl.DataFrame(assessments, schema_overrides={"form_correct": pl.Int64}).group_by("word_id", "direction").agg(
            pl.len().alias("attempts"), pl.col("meaning_correct").sum().alias("meaning_correct"),
            pl.col("target_used").sum().alias("target_used"),
            pl.col("form_correct").count().alias("form_attempts"), (pl.col("form_correct") == 0).sum().alias("form_failures"),
            ((pl.col("meaning_correct") == 1) & (pl.col("form_correct").is_null() | (pl.col("form_correct") == 1))).sum().alias("correct"))
        values = {(row["word_id"], row["direction"]): row for row in grades.to_dicts()}
    state_map = {(row["word_id"], row["direction"]): row for row in states}
    result = []
    for row in frame.to_dicts():
        key = row["word_id"], row["direction"]
        missing = {name: 0 for name in ("attempts", "meaning_correct", "target_used", "form_attempts", "form_failures", "correct")}
        result.append({**row, **values.get(key, missing), "due_at": state_map.get(key, {}).get("due_at")})
    return result
