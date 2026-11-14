"""Share lexical review evidence and FSRS state between practice modes.

Example: a sentence confusion receives the same Again update as a failed word quiz, with its real source preserved.
"""

from __future__ import annotations

import json
from datetime import datetime


SCHEMA = """
CREATE TABLE IF NOT EXISTS word_transfers (
    id INTEGER PRIMARY KEY,
    word_id INTEGER NOT NULL REFERENCES vocab_words(id),
    lookup_id INTEGER NOT NULL REFERENCES vocab_lookups(id),
    direction TEXT NOT NULL CHECK(direction IN ('nl_en','en_nl')),
    sentence_id INTEGER REFERENCES sentence_presentations(id),
    verb_id INTEGER REFERENCES verb_presentations(id),
    correct INTEGER NOT NULL CHECK(correct IN (0,1)),
    answered_at REAL NOT NULL,
    elapsed_ms INTEGER NOT NULL,
    scheduler_json TEXT NOT NULL,
    include_early INTEGER NOT NULL CHECK(include_early IN (0,1)),
    scheduled INTEGER NOT NULL CHECK(scheduled IN (0,1)),
    rating INTEGER,recall REAL,due_at REAL,before_json TEXT,after_json TEXT,log_json TEXT,
    CHECK((sentence_id IS NOT NULL)+(verb_id IS NOT NULL)=1),
    UNIQUE(sentence_id,word_id,direction),UNIQUE(verb_id,word_id,direction)
);
CREATE INDEX IF NOT EXISTS idx_word_transfers_skill ON word_transfers(word_id,direction,answered_at);
"""

# Immutable source events stay in their own tables; this is the shared chronological review ledger.
REVIEWS = """
SELECT 'practice' AS origin,p.id AS event_id,l.word_id,q.lookup_id,s.direction,p.answered_at,p.elapsed_ms,
       p.outcome='correct' AS correct,p.unsure,s.scheduler_json,s.include_early,p.outcome,
       json_extract(q.body_json,'$.surface') AS word
FROM practice_presentations p JOIN practice_questions q ON q.id=p.question_id
JOIN practice_sessions s ON s.id=p.session_id JOIN vocab_lookups l ON l.id=q.lookup_id
WHERE p.selected_idx IS NOT NULL
UNION ALL
SELECT CASE WHEN r.sentence_id IS NULL THEN 'verb' ELSE 'sentence' END,r.id,r.word_id,r.lookup_id,r.direction,
       r.answered_at,r.elapsed_ms,r.correct,0,r.scheduler_json,r.include_early,
       CASE WHEN COALESCE(s.outcome,v.outcome)='invalid' THEN 'invalid'
            WHEN r.correct=1 THEN 'correct' ELSE 'wrong' END,l.lemma
FROM word_transfers r JOIN vocab_lookups l ON l.id=r.lookup_id
LEFT JOIN sentence_presentations s ON s.id=r.sentence_id LEFT JOIN verb_presentations v ON v.id=r.verb_id
"""


class WordReviewDbMixin:
    """Own shared lexical scheduling without manufacturing quiz answers; e.g. one typed answer remains one activity event."""

    def word_candidates(self, direction: str) -> list[dict]:
        """Supply identical word skills to quiz and sentence selectors; e.g. veilig has the same due date in both modes."""

        from yt_whisper_subs import vocabulary_db

        sql = f"""
        WITH history AS ({REVIEWS}), totals AS (
            SELECT word_id,COUNT(*) AS attempts,SUM(outcome='wrong') AS failures
            FROM history WHERE direction=:direction AND outcome IN ('correct','wrong') GROUP BY word_id
        ) SELECT r.*,s.card_json,s.due_at,COALESCE(t.attempts,0) AS attempts,COALESCE(t.failures,0) AS failures
          FROM ({vocabulary_db.READY_WORDS}) r
          LEFT JOIN practice_states s ON s.word_id=r.word_id AND s.direction=:direction
          LEFT JOIN totals t ON t.word_id=r.word_id
        """
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(sql, {"direction": direction})]

    def _word_observe(self, conn, kind: str, row: dict, lookup_id: int, correct: bool) -> None:
        """Commit one transferable lexical observation; e.g. both sides of a confusion receive Again without duplicate reviews."""

        from yt_whisper_subs import practice

        word_id = conn.execute("SELECT word_id FROM vocab_lookups WHERE id=?", (lookup_id,)).fetchone()[0]
        direction = row.get("direction", "en_nl")
        source = "sentence_id" if kind == "sentence" else "verb_id"
        if conn.execute(f"SELECT 1 FROM word_transfers WHERE {source}=? AND word_id=? AND direction=?",
                        (row["id"], word_id, direction)).fetchone():
            return
        early = json.loads(row.get("options_json", "{}" )).get("include_early", False)
        state = conn.execute("SELECT card_json,due_at FROM practice_states WHERE word_id=? AND direction=?", (word_id, direction)).fetchone()
        result = None
        last = json.loads(state["card_json"]).get("last_review") if state else None
        historical = last is not None and datetime.fromisoformat(last).timestamp() > row["answered_at"]
        if not historical and (not correct or state is None or state["due_at"] <= row["answered_at"] or early):
            policy = practice.ReviewPolicy(row["scheduler_json"])
            card = state["card_json"] if state else policy.new_card(word_id * 2 + (direction == "en_nl"), row["answered_at"])
            result = policy.answer(card, correct=correct, unsure=False, now=row["answered_at"], elapsed_ms=row["elapsed_ms"])
            self._practice_state(conn, {"word_id": word_id, "direction": direction}, result)
        keys = ("rating", "recall", "due_at", "before_json", "after_json", "log_json")
        conn.execute(f"INSERT INTO word_transfers(word_id,lookup_id,direction,{source},correct,answered_at,elapsed_ms,"
                     "scheduler_json,include_early,scheduled,rating,recall,due_at,before_json,after_json,log_json) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (word_id, lookup_id, direction, row["id"], int(correct), row["answered_at"], row["elapsed_ms"],
                      row["scheduler_json"], int(early), int(result is not None), *[result[key] if result else None for key in keys]))
        if historical:
            self._word_replay(conn, word_id, direction)

    def _word_sync(self, conn) -> None:
        """Bring recorded sentence word assessments into the common ledger once; e.g. earlier typed successes are retained without regrading."""

        rows = conn.execute(
            "SELECT p.id,p.answered_at,p.elapsed_ms,s.direction,s.options_json,s.scheduler_json,a.lookup_id,l.word_id,"
            "a.meaning_correct,a.form_correct,a.target_used FROM sentence_assessments a "
            "JOIN sentence_presentations p ON p.id=a.presentation_id JOIN sentence_sessions s ON s.id=p.session_id "
            "JOIN vocab_lookups l ON l.id=a.lookup_id WHERE p.outcome IN ('correct','wrong') "
            "AND (a.target_used=1 OR a.meaning_correct=0 OR a.form_correct=0) "
            "AND NOT EXISTS(SELECT 1 FROM word_transfers r WHERE r.sentence_id=p.id AND r.word_id=l.word_id) "
            "ORDER BY p.answered_at,p.id").fetchall()
        affected = set()
        for row in rows:
            correct = bool(row["meaning_correct"]) and row["form_correct"] != 0
            early = json.loads(row["options_json"])["include_early"]
            conn.execute("INSERT INTO word_transfers(word_id,lookup_id,direction,sentence_id,correct,answered_at,elapsed_ms,"
                         "scheduler_json,include_early,scheduled) VALUES(?,?,?,?,?,?,?,?,?,0)",
                         (row["word_id"],row["lookup_id"],row["direction"],row["id"],int(correct),row["answered_at"],row["elapsed_ms"],
                          row["scheduler_json"],int(early)))
            affected.add((row["word_id"], row["direction"]))
        for word_id, direction in affected:
            self._word_replay(conn, word_id, direction)

    def _word_replay(self, conn, word_id: int, direction: str) -> None:
        """Rebuild from all valid modes in time order; e.g. reporting a sentence removes its failures while retaining subsequent quiz successes."""

        from yt_whisper_subs import practice

        rows = conn.execute(f"SELECT * FROM ({REVIEWS}) WHERE word_id=? AND direction=? AND outcome IN ('correct','wrong') "
                            "ORDER BY answered_at,origin,event_id", (word_id, direction)).fetchall()
        conn.execute("DELETE FROM practice_states WHERE word_id=? AND direction=?", (word_id, direction))
        card, due = None, 0
        for row in rows:
            if row["origin"] != "practice" and row["correct"] and card and due > row["answered_at"] and not row["include_early"]:
                continue
            policy = practice.ReviewPolicy(row["scheduler_json"])
            card = card or policy.new_card(word_id * 2 + (direction == "en_nl"), row["answered_at"])
            result = policy.answer(card, correct=bool(row["correct"]), unsure=bool(row["unsure"]),
                                   now=row["answered_at"], elapsed_ms=row["elapsed_ms"])
            card, due = result["after_json"], result["due_at"]
            self._practice_state(conn, {"word_id": word_id, "direction": direction}, result)

    def _word_unlink(self, conn, kind: str, question_id: int) -> None:
        """Replay every affected lexical skill after a report; e.g. newly added confused words lose invalid review influence too."""

        source = "sentence_id" if kind == "sentence" else "verb_id"
        rows = conn.execute(f"SELECT DISTINCT r.word_id,r.direction FROM word_transfers r JOIN {kind}_presentations p "
                            f"ON p.id=r.{source} WHERE p.question_id=?", (question_id,)).fetchall()
        for row in rows:
            self._word_replay(conn, row["word_id"], row["direction"])
