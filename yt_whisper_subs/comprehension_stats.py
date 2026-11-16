"""Derive comprehension and writing trends from immutable submitted answers.

Example: a 50% content answer contributes progress while an unavailable correction contributes no invented zero.
"""

from datetime import datetime

import polars as pl


def snapshot(db, direction: str) -> dict:
    """Keep modality, question format and language scores distinct; e.g. reading success does not claim listening mastery."""

    with db._connect() as conn:
        answers = [dict(row) for row in conn.execute("SELECT p.*,q.kind,json_extract(q.body_json,'$.skill') AS skill,"
                    "json_extract(q.body_json,'$.prompt') AS prompt FROM comprehension_presentations p "
                    "JOIN comprehension_questions q ON q.id=p.question_id JOIN comprehension_sessions s ON s.id=p.session_id "
                    "WHERE s.direction=? AND p.answer_text IS NOT NULL ORDER BY p.answered_at", (direction,))]
        words = [dict(row) for row in conn.execute("SELECT l.lemma,a.direction,COUNT(*) AS assessed,SUM(a.correct=0) AS failures,"
                    "AVG(a.correct) AS accuracy,st.due_at FROM comprehension_assessments a JOIN vocab_lookups l ON l.id=a.lookup_id "
                    "JOIN comprehension_presentations p ON p.id=a.presentation_id JOIN comprehension_sessions s ON s.id=p.session_id "
                    "LEFT JOIN practice_states st ON st.word_id=a.word_id AND st.direction=a.direction "
                    "WHERE s.direction=? AND p.outcome IN ('correct','wrong') GROUP BY a.word_id,a.direction ORDER BY failures DESC", (direction,))]
        errors = [dict(row) for row in conn.execute("SELECT e.body_json,p.answered_at FROM comprehension_errors e "
                    "JOIN comprehension_presentations p ON p.id=e.presentation_id JOIN comprehension_sessions s ON s.id=p.session_id "
                    "WHERE s.direction=? AND p.outcome IN ('correct','wrong') ORDER BY p.answered_at DESC", (direction,))]
        rounds = [dict(row) for row in conn.execute("SELECT s.created_at,s.status,COUNT(p.id) AS planned,COUNT(p.answer_text) AS submitted,"
                    "AVG(p.score) AS score FROM comprehension_sessions s JOIN comprehension_presentations p ON p.session_id=s.id "
                    "WHERE s.direction=? AND s.shown_at IS NOT NULL GROUP BY s.id ORDER BY s.created_at DESC", (direction,))]
        plays = conn.execute('SELECT COUNT(*) FROM comprehension_plays p JOIN comprehension_sessions s ON s.id=p.session_id WHERE s.direction=?', (direction,)).fetchone()[0]
    valid = [row for row in answers if row['outcome'] in {'correct', 'wrong'}]
    daily, skills = [], []
    if valid:
        data = [dict(day=datetime.fromtimestamp(row['answered_at']).date().isoformat(), kind=row['kind'],
                     score=row['score'], skill=row['skill'], correct=row['outcome'] == 'correct') for row in valid]
        frame = pl.DataFrame(data)
        daily = frame.group_by('day', 'kind').agg(pl.len().alias('answers'), pl.col('score').mean(), pl.col('correct').sum()).sort('day').to_dicts()
        skills = frame.group_by('skill').agg(pl.len().alias('answers'), pl.col('score').mean(), pl.col('correct').sum()).sort('score').to_dicts()
    language = [row for row in valid if row['language_correct'] is not None]
    return dict(submitted=len(answers), graded=len(valid), correct=sum(row['outcome'] == 'correct' for row in valid),
                pending=sum(row['outcome'] is None for row in answers), score=sum(row['score'] for row in valid) / len(valid) if valid else None,
                language=sum(row['language_correct'] for row in language) / len(language) if language else None,
                language_answers=len(language), answers=answers, words=words, errors=errors, rounds=rounds, daily=daily, skills=skills, plays=plays)
