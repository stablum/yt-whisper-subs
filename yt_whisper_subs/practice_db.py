"""Relational practice history and transactional review state in the vocabulary catalog.

Example: planned questions do not count as presentations until `practice_current()` displays them.
"""

from __future__ import annotations

import json
import time
import uuid

from yt_whisper_subs import vocabulary_db


SCHEMA = """
CREATE TABLE IF NOT EXISTS practice_questions (
    id INTEGER PRIMARY KEY,
    lookup_id INTEGER NOT NULL REFERENCES vocab_lookups(id),
    direction TEXT NOT NULL CHECK(direction IN ('nl_en','en_nl','sound_nl')),
    body_json TEXT NOT NULL,
    validation_json TEXT NOT NULL,
    model TEXT NOT NULL,
    reasoning TEXT NOT NULL,
    prompt_version INTEGER NOT NULL,
    created_at REAL NOT NULL,
    flagged_at REAL
);
CREATE INDEX IF NOT EXISTS idx_practice_questions ON practice_questions(lookup_id,direction,flagged_at);
CREATE TABLE IF NOT EXISTS practice_sessions (
    id TEXT PRIMARY KEY,
    direction TEXT NOT NULL CHECK(direction IN ('nl_en','en_nl','sound_nl')),
    requested_count INTEGER NOT NULL CHECK(requested_count BETWEEN 1 AND 50),
    include_early INTEGER NOT NULL CHECK(include_early IN (0,1)),
    created_at REAL NOT NULL,
    finished_at REAL,
    status TEXT NOT NULL CHECK(status IN ('active','completed','stopped')),
    scheduler_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS practice_presentations (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES practice_sessions(id),
    question_id INTEGER NOT NULL REFERENCES practice_questions(id),
    ordinal INTEGER NOT NULL CHECK(ordinal >= 1),
    choices_json TEXT NOT NULL,
    correct_idx INTEGER NOT NULL CHECK(correct_idx BETWEEN 0 AND 2),
    shown_at REAL,
    answered_at REAL,
    selected_idx INTEGER CHECK(selected_idx BETWEEN 0 AND 2),
    elapsed_ms INTEGER CHECK(elapsed_ms >= 0),
    unsure INTEGER CHECK(unsure IN (0,1)),
    outcome TEXT CHECK(outcome IN ('correct','wrong','skipped','invalid')),
    rating INTEGER CHECK(rating BETWEEN 1 AND 4),
    recall REAL CHECK(recall BETWEEN 0 AND 1),
    due_at REAL,
    before_json TEXT,
    after_json TEXT,
    log_json TEXT,
    UNIQUE(session_id,ordinal)
);
CREATE INDEX IF NOT EXISTS idx_practice_presentations_question ON practice_presentations(question_id);
CREATE INDEX IF NOT EXISTS idx_practice_presentations_date ON practice_presentations(shown_at);
CREATE INDEX IF NOT EXISTS idx_practice_answers ON practice_presentations(answered_at)
    WHERE answered_at IS NOT NULL AND selected_idx IS NOT NULL;
CREATE TABLE IF NOT EXISTS practice_states (
    word_id INTEGER NOT NULL REFERENCES vocab_words(id),
    direction TEXT NOT NULL CHECK(direction IN ('nl_en','en_nl','sound_nl')),
    card_json TEXT NOT NULL,
    due_at REAL NOT NULL,
    PRIMARY KEY(word_id,direction)
);
"""

PRESENTATION = """
SELECT p.*,q.body_json,q.lookup_id,l.word_id,l.context_json,s.direction,s.status,s.scheduler_json,s.requested_count
FROM practice_presentations p JOIN practice_questions q ON q.id=p.question_id
JOIN vocab_lookups l ON l.id=q.lookup_id JOIN practice_sessions s ON s.id=p.session_id
"""

# The track keys match practice_tracks.Track; each answer column is the durable submission evidence.
_DAY_SOURCES = (
    ('practice', "'practice:' || s.direction", 'selected_idx'),
    ('verb', "'verb'", 'answer_text'),
    ('sentence', "'sentence:' || s.direction", 'answer_text'),
    ('word_order', "CASE s.variant WHEN 'phrases' THEN 'word_order:phrases' ELSE 'word_order' END", 'answer_json'),
    ('comprehension', "'comprehension:' || s.direction", 'answer_text'),
)

class PracticeDbMixin:
    """Keep immutable question/answer evidence alongside a rebuildable scheduling cache."""

    def practice_has_candidates(self) -> bool:
        """Check eligibility without loading the scheduler; e.g. avoid reminders before any Dutch words are saved."""

        with self._connect() as conn:
            return bool(conn.execute(f"SELECT EXISTS(SELECT 1 {vocabulary_db.READY_LOOKUPS})").fetchone()[0])

    def practice_last_answer(self, until: float) -> float | None:
        """Use the answer index for cheap reminder checks; e.g. reporting an unanswered question is not practice."""

        with self._connect() as conn:
            row = conn.execute("SELECT MAX(stamp) FROM (SELECT (SELECT answered_at FROM practice_presentations "
                               "WHERE answered_at IS NOT NULL AND selected_idx IS NOT NULL AND answered_at<=? "
                               "ORDER BY answered_at DESC LIMIT 1) AS stamp UNION ALL "
                               "SELECT (SELECT answered_at FROM verb_presentations WHERE answered_at IS NOT NULL "
                               "AND answer_text IS NOT NULL AND answered_at<=? ORDER BY answered_at DESC LIMIT 1) UNION ALL "
                               "SELECT (SELECT answered_at FROM sentence_presentations WHERE answered_at IS NOT NULL "
                               "AND answer_text IS NOT NULL AND answered_at<=? ORDER BY answered_at DESC LIMIT 1) UNION ALL "
                               "SELECT (SELECT answered_at FROM comprehension_presentations WHERE answer_text IS NOT NULL "
                               "AND answered_at<=? ORDER BY answered_at DESC LIMIT 1) UNION ALL "
                               "SELECT (SELECT answered_at FROM word_order_presentations WHERE answer_json IS NOT NULL "
                               "AND answered_at<=? ORDER BY answered_at DESC LIMIT 1))",
                               (until, until, until, until, until)).fetchone()
        return row[0]

    def practice_activity_rows(self, since: float, until: float) -> list[dict]:
        """Read real answer events in a bounded interval; e.g. seven days for a reminder's chart."""

        with self._connect() as conn:
            rows = conn.execute("SELECT p.answered_at,p.selected_idx,NULL AS answer_text,p.outcome,s.direction,l.word_id,NULL AS word_ids_json "
                                "FROM practice_presentations p JOIN practice_sessions s ON s.id=p.session_id "
                                "JOIN practice_questions q ON q.id=p.question_id JOIN vocab_lookups l ON l.id=q.lookup_id "
                                "WHERE p.answered_at IS NOT NULL AND p.selected_idx IS NOT NULL "
                                "AND p.answered_at>=? AND p.answered_at<=? UNION ALL "
                                "SELECT p.answered_at,NULL,p.answer_text,p.outcome,'verbs',t.verb_id,NULL "
                                "FROM verb_presentations p JOIN verb_questions q ON q.id=p.question_id "
                                "JOIN verb_targets t ON t.id=q.target_id WHERE p.answer_text IS NOT NULL "
                                "AND p.answered_at>=? AND p.answered_at<=? UNION ALL "
                                "SELECT p.answered_at,NULL,p.answer_text,p.outcome,'sentence_'||q.direction,q.anchor_word_id,"
                                "(SELECT json_group_array(l.word_id) FROM sentence_question_words w "
                                "JOIN vocab_lookups l ON l.id=w.lookup_id WHERE w.question_id=q.id) "
                                "FROM sentence_presentations p JOIN sentence_questions q ON q.id=p.question_id "
                                "WHERE p.answer_text IS NOT NULL AND p.answered_at>=? AND p.answered_at<=? UNION ALL "
                                "SELECT p.answered_at,p.selected_idx,p.answer_text,p.outcome,'comprehension_'||s.direction,0,"
                                "json_extract(q.body_json,'$.word_ids') FROM comprehension_presentations p "
                                "JOIN comprehension_sessions s ON s.id=p.session_id JOIN comprehension_questions q ON q.id=p.question_id "
                                "WHERE p.answer_text IS NOT NULL AND p.answered_at>=? AND p.answered_at<=? UNION ALL "
                                "SELECT p.answered_at,NULL,p.answer_json,p.outcome,"
                                "CASE q.variant WHEN 'phrases' THEN 'phrase_order' ELSE 'word_order' END,l.word_id,NULL "
                                "FROM word_order_presentations p JOIN word_order_questions q ON q.id=p.question_id "
                                "JOIN vocab_lookups l ON l.id=q.lookup_id WHERE p.answer_json IS NOT NULL "
                                "AND p.answered_at>=? AND p.answered_at<=? ORDER BY answered_at",
                                (since, until, since, until, since, until, since, until, since, until))
            return [dict(row) for row in rows]

    def practice_day_states(self, since: float, until: float) -> dict[str, bool]:
        """Read attempted tracks and whether they finished a round in the interval; e.g. ignore previews and early stops."""

        events = '\nUNION ALL\n'.join(f"""
            SELECT {track} AS track, 0 AS completed
            FROM {kind}_presentations p JOIN {kind}_sessions s ON s.id=p.session_id
            WHERE p.answered_at BETWEEN :since AND :until AND p.{answer} IS NOT NULL
            UNION ALL
            SELECT {track}, 1 FROM {kind}_sessions s
            WHERE s.finished_at BETWEEN :since AND :until AND s.status='completed'
              AND EXISTS(SELECT 1 FROM {kind}_presentations p
                         WHERE p.session_id=s.id AND p.{answer} IS NOT NULL)
        """ for kind, track, answer in _DAY_SOURCES)
        with self._connect() as conn:
            rows = conn.execute(f"SELECT track, MAX(completed) FROM ({events}) GROUP BY track",
                                {'since': since, 'until': until})
            return {track: bool(completed) for track, completed in rows}

    def practice_cached(self, lookup_id: int, direction: str, policy: dict) -> list[dict]:
        """Reuse only the requested generation policy; e.g. a model upgrade cannot silently serve older choices."""

        params = {"lookup_id": lookup_id, "direction": direction, **policy}
        with self._connect() as conn:
            rows = conn.execute("SELECT q.*,COUNT(p.shown_at) AS uses FROM practice_questions q "
                                "LEFT JOIN practice_presentations p ON p.question_id=q.id "
                                "WHERE q.lookup_id=:lookup_id AND q.direction=:direction AND q.prompt_version=:prompt_version "
                                "AND q.model=:model AND q.reasoning=:reasoning AND q.flagged_at IS NULL "
                                "GROUP BY q.id ORDER BY uses,q.id DESC", params)
            return [dict(row) for row in rows]

    def practice_cache(self, item: dict) -> dict:
        """Persist only approved content and its provenance; e.g. preserve the independent review verdict."""

        with self._connect() as conn:
            cols = ("lookup_id", "direction", "body_json", "validation_json", "model", "reasoning", "prompt_version")
            values = [item[key] for key in cols]
            cursor = conn.execute("INSERT INTO practice_questions "
                                  "(lookup_id,direction,body_json,validation_json,model,reasoning,prompt_version,created_at) "
                                  "VALUES(?,?,?,?,?,?,?,?)", (*values, time.time()))
            return dict(conn.execute("SELECT * FROM practice_questions WHERE id=?", (cursor.lastrowid,)).fetchone())

    def practice_create(self, opts: dict, questions: list[dict], scheduler_json: str) -> str:
        """Create a complete round atomically; e.g. an API error never leaves half a playable round."""

        if len(questions) != opts["count"]:
            raise ValueError("A word-quiz round must contain its requested number of approved questions")
        session_id = uuid.uuid4().hex
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE practice_sessions SET status='stopped',finished_at=? WHERE status='active' AND direction=?",
                         (time.time(), opts["direction"]))
            self._practice_consume_stock(conn, "practice", opts["direction"])
            conn.execute("INSERT INTO practice_sessions VALUES(?,?,?,?,?,?,?,?)",
                         (session_id, opts["direction"], opts["count"], int(opts["include_early"]),
                          time.time(), None, "active", scheduler_json))
            for idx, row in enumerate(questions, 1):
                conn.execute("INSERT INTO practice_presentations "
                             "(session_id,question_id,ordinal,choices_json,correct_idx) VALUES(?,?,?,?,?)",
                             (session_id, row["id"], idx, json.dumps(row["choices"], ensure_ascii=False), row["correct_idx"]))
        return session_id

    @staticmethod
    def _practice_row(row) -> dict | None:
        """Decode a stored presentation in one place; e.g. retain exactly the choices originally displayed."""

        if row is None:
            return None
        result = dict(row)
        result["content"] = json.loads(result.pop("body_json"))
        result["lookup_context"] = json.loads(result.pop("context_json"))
        result["choices"] = json.loads(result.pop("choices_json"))
        return result

    def practice_active(self, direction: str | None = None) -> str | None:
        """Find a resumable round without altering it; e.g. the app closed between questions."""

        with self._connect() as conn:
            row = conn.execute("SELECT id FROM practice_sessions WHERE status='active' AND (? IS NULL OR direction=?) "
                               "ORDER BY created_at DESC LIMIT 1", (direction, direction)).fetchone()
            return row[0] if row else None

    def practice_current(self, session_id: str) -> dict | None:
        """Count a question only when shown and preserve its first display time; e.g. resume after a crash."""

        from yt_whisper_subs import practice

        query = (PRESENTATION + " WHERE p.session_id=? AND p.outcome IS NULL "
                 "AND s.status='active' ORDER BY p.ordinal LIMIT 1")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            skipped = False
            while row := conn.execute(query, (session_id,)).fetchone():
                current = self._practice_row(row)
                try:
                    choices = practice.display_choices(current["choices"])
                except ValueError:
                    # A saved set can only be offered if its visible choices remain distinct.
                    conn.execute("UPDATE practice_presentations SET outcome='invalid' WHERE id=?", (row["id"],))
                    skipped = True
                    continue
                display = row["choices_json"] if choices == current["choices"] else json.dumps(choices, ensure_ascii=False)
                current["choices"] = choices
                current["shown_at"] = row["shown_at"] if row["shown_at"] is not None else time.time()
                conn.execute("UPDATE practice_presentations SET shown_at=?,choices_json=? WHERE id=?",
                             (current["shown_at"], display, row["id"]))
                return current
            if skipped:
                self._practice_complete(conn)
            return None

    @staticmethod
    def _practice_state(conn, row: dict, result: dict) -> None:
        """Update the derived schedule in the answer transaction; e.g. simultaneous players cannot lose reviews."""

        conn.execute("INSERT INTO practice_states VALUES(?,?,?,?) ON CONFLICT(word_id,direction) "
                     "DO UPDATE SET card_json=excluded.card_json,due_at=excluded.due_at",
                     (row["word_id"], row["direction"], result["after_json"], result["due_at"]))

    @staticmethod
    def _practice_complete(conn) -> None:
        """Finish rounds when their last item is resolved; e.g. closing on final feedback still leaves a completed result."""

        conn.execute("UPDATE practice_sessions SET status='completed',finished_at=? WHERE status='active' "
                     "AND NOT EXISTS(SELECT 1 FROM practice_presentations p "
                     "WHERE p.session_id=practice_sessions.id AND p.outcome IS NULL)", (time.time(),))

    def practice_answer(self, presentation_id: int, choice: int, unsure: bool, elapsed_ms: int) -> dict:
        """Commit one answer and its FSRS state together; e.g. duplicate button signals are idempotent."""

        from yt_whisper_subs import practice

        if type(choice) is not int or choice not in range(3) or type(elapsed_ms) is not int or elapsed_ms < 0:
            raise ValueError("Invalid practice answer")
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(PRESENTATION + " WHERE p.id=?", (presentation_id,)).fetchone()
            if row is None or row["shown_at"] is None:
                raise ValueError("This question has not been presented")
            if row["outcome"] is not None:
                return self._practice_row(row)
            if row["status"] != "active":
                raise ValueError("This practice round has ended")
            state = conn.execute("SELECT card_json FROM practice_states WHERE word_id=? AND direction=?",
                                 (row["word_id"], row["direction"])).fetchone()
            correct = choice == row["correct_idx"]
            card_id = practice.ReviewPolicy.word_card_id(row["word_id"], row["direction"])
            card_json = state[0] if state else practice.ReviewPolicy.new_card(card_id, now)
            result = practice.ReviewPolicy(row["scheduler_json"]).answer(
                card_json, correct=correct, unsure=bool(unsure), now=now, elapsed_ms=elapsed_ms)
            self._practice_state(conn, row, result)
            conn.execute("UPDATE practice_presentations SET answered_at=?,selected_idx=?,elapsed_ms=?,unsure=?,"
                         "outcome=?,rating=?,recall=?,due_at=?,before_json=?,after_json=?,log_json=? WHERE id=?",
                         (now, choice, elapsed_ms, int(unsure), "correct" if correct else "wrong", result["rating"],
                          result["recall"], result["due_at"], result["before_json"], result["after_json"],
                          result["log_json"], presentation_id))
            self._practice_complete(conn)
            completed = conn.execute(PRESENTATION + " WHERE p.id=?", (presentation_id,)).fetchone()
            self._accretion_mistake(conn, "quiz", dict(completed))
            return self._practice_row(completed)

    def practice_flag(self, presentation_id: int) -> None:
        """Quarantine an unclear question and remove its grading influence; e.g. report an AI synonym error."""

        from yt_whisper_subs import practice

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(PRESENTATION + " WHERE p.id=?", (presentation_id,)).fetchone()
            if row is None or row["shown_at"] is None:
                raise ValueError("This question has not been presented")
            conn.execute("UPDATE practice_questions SET flagged_at=? WHERE id=?", (time.time(), row["question_id"]))
            # Every use of a defective question is excluded, preserving the original answer evidence.
            conn.execute("UPDATE practice_presentations SET outcome='invalid',"
                         "answered_at=CASE WHEN shown_at IS NOT NULL THEN COALESCE(answered_at,?) ELSE answered_at END "
                         "WHERE question_id=?", (time.time(), row["question_id"]))
            self._word_replay(conn, row["word_id"], row["direction"])
            self._accretion_unlink(conn, "quiz", row["question_id"])
            self._practice_complete(conn)

    def practice_finish(self, session_id: str) -> dict:
        """Finish or stop without inventing answers for unseen terms; e.g. end a round early."""

        with self._connect() as conn:
            remaining = conn.execute("SELECT COUNT(*) FROM practice_presentations WHERE session_id=? AND outcome IS NULL",
                                     (session_id,)).fetchone()[0]
            conn.execute("UPDATE practice_sessions SET status=?,finished_at=? WHERE id=? AND status='active'",
                         ("stopped" if remaining else "completed", time.time(), session_id))
        return self.practice_summary(session_id)

    def practice_summary(self, session_id: str) -> dict:
        """Derive results from actual presentations; e.g. skipped/invalid items never count as failures."""

        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS planned,COUNT(shown_at) AS shown,"
                               "COALESCE(SUM(outcome='correct'),0) AS correct,COALESCE(SUM(outcome='wrong'),0) AS wrong,"
                               "COALESCE(SUM(outcome='invalid'),0) AS invalid FROM practice_presentations WHERE session_id=?",
                               (session_id,)).fetchone()
            return dict(row)
