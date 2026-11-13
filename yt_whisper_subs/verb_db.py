"""Atomic verb practice lifecycle with immutable submissions and rebuildable schedules.

Example: submission is saved before remote grading, so retrying after a timeout never adds another attempt.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from yt_whisper_subs import verb_practice as verbs


PRESENTATION = """
SELECT p.*,q.body_json,q.target_id,q.flagged_at,t.verb_id,t.tense,t.subject,t.structure,
       v.lemma,v.family,v.transitive,s.status,s.scheduler_json
FROM verb_presentations p JOIN verb_questions q ON q.id=p.question_id
JOIN verb_targets t ON t.id=q.target_id JOIN verb_lemmas v ON v.id=t.verb_id
JOIN verb_sessions s ON s.id=p.session_id
"""


class VerbDbMixin:
    """Own typed practice transactions independently of vocabulary multiple choice."""

    def verb_has_history(self) -> bool:
        """Enable reminders once verb practice is used; e.g. an empty vocabulary need not disable an established verb habit."""

        with self._connect() as conn:
            return bool(conn.execute("SELECT EXISTS(SELECT 1 FROM verb_sessions)").fetchone()[0])

    def verb_catalog(self) -> list[dict]:
        """Seed the curated syllabus idempotently; e.g. verbs are usable before any subtitle words are captured."""

        from yt_whisper_subs import verb_syllabus

        with self._connect() as conn:
            conn.executemany("INSERT OR IGNORE INTO verb_lemmas(lemma,family,transitive) VALUES(?,?,?)",
                             verb_syllabus.CATALOG)
            return [dict(row) for row in conn.execute("SELECT * FROM verb_lemmas ORDER BY id")]

    def verb_targets(self) -> list[dict]:
        """Load only targets encountered so far; e.g. do not materialize the entire grammar cross-product in SQL."""

        with self._connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT t.id AS target_id,t.verb_id,t.tense,t.subject,t.structure,s.card_json,s.due_at "
                "FROM verb_targets t LEFT JOIN verb_states s ON s.target_id=t.id")]

    def verb_target(self, row: dict) -> dict:
        """Assign a stable relational target key; e.g. past-perfect wij inversion has one schedule per verb."""

        values = tuple(row[key] for key in ("verb_id", "tense", "subject", "structure"))
        with self._connect() as conn:
            conn.execute("INSERT OR IGNORE INTO verb_targets(verb_id,tense,subject,structure) VALUES(?,?,?,?)", values)
            target_id = conn.execute("SELECT id FROM verb_targets WHERE verb_id=? AND tense=? AND subject=? AND structure=?",
                                     values).fetchone()[0]
        return {**row, "target_id": target_id}

    def verb_cached(self, target_id: int, policy: dict) -> list[dict]:
        """Rotate approved same-policy variants; e.g. a model change cannot silently reuse old linguistic checks."""

        with self._connect() as conn:
            rows = conn.execute(
                "SELECT q.*,COUNT(p.shown_at) AS uses FROM verb_questions q "
                "LEFT JOIN verb_presentations p ON p.question_id=q.id WHERE q.target_id=:target_id "
                "AND q.model=:model AND q.reasoning=:reasoning AND q.prompt_version=:prompt_version "
                "AND q.flagged_at IS NULL GROUP BY q.id ORDER BY uses,q.id DESC", {"target_id": target_id, **policy})
            return [dict(row) for row in rows]

    def verb_cache(self, draft: verbs.Draft, verdict: verbs.Verdict, policy: dict) -> dict:
        """Store independently approved content and provenance; e.g. rejected questions never become playable."""

        if not verdict.valid or draft.target_id != verdict.target_id:
            raise ValueError("Only approved verb questions can be cached")
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO verb_questions(target_id,body_json,validation_json,model,reasoning,prompt_version,created_at) "
                "VALUES(?,?,?,?,?,?,?)", (draft.target_id, draft.model_dump_json(), verdict.model_dump_json(),
                                         policy["model"], policy["reasoning"], policy["prompt_version"], time.time()))
            return dict(conn.execute("SELECT * FROM verb_questions WHERE id=?", (cursor.lastrowid,)).fetchone())

    def verb_create(self, opts: verbs.Options, questions: list[dict], scheduler_json: str) -> str:
        """Replace a round only after preparation succeeds; e.g. cancelling generation leaves the old round intact."""

        if not questions:
            raise ValueError("No approved verb transformations are available")
        session_id, now = uuid.uuid4().hex, time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE verb_sessions SET status='stopped',finished_at=? WHERE status='active'", (now,))
            conn.execute("INSERT INTO verb_sessions VALUES(?,?,?,?,?,?)",
                         (session_id, opts.model_dump_json(), scheduler_json, now, None, "active"))
            conn.executemany("INSERT INTO verb_presentations(session_id,question_id,ordinal) VALUES(?,?,?)",
                             [(session_id, row["id"], idx) for idx, row in enumerate(questions, 1)])
        return session_id

    def verb_active(self) -> str | None:
        """Find a resumable typed round without exposing a question; e.g. browsing statistics is read-only."""

        with self._connect() as conn:
            row = conn.execute("SELECT id FROM verb_sessions WHERE status='active' ORDER BY created_at DESC LIMIT 1").fetchone()
        return row[0] if row else None

    @staticmethod
    def _verb_row(row) -> dict | None:
        """Decode exact linguistic snapshots in one place; e.g. resume uses the original instruction."""

        if row is None:
            return None
        result = dict(row)
        result["content"] = json.loads(result.pop("body_json"))
        result["grade"] = json.loads(result["grade_json"]) if result["grade_json"] else None
        return result

    def verb_current(self, session_id: str) -> dict | None:
        """Record exposure only for a visible question; e.g. an unfinished pending submission resumes unchanged."""

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(PRESENTATION + " WHERE p.session_id=? AND p.outcome IS NULL AND s.status='active' "
                               "ORDER BY p.ordinal LIMIT 1", (session_id,)).fetchone()
            if row is None:
                return None
            conn.execute("UPDATE verb_presentations SET shown_at=COALESCE(shown_at,?) WHERE id=?", (time.time(), row["id"]))
            return self._verb_row(conn.execute(PRESENTATION + " WHERE p.id=?", (row["id"],)).fetchone())

    def verb_get(self, presentation_id: int) -> dict:
        """Read an existing attempt without changing exposure; e.g. retry only the failed correction request."""

        with self._connect() as conn:
            row = conn.execute(PRESENTATION + " WHERE p.id=?", (presentation_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown verb exercise")
        return self._verb_row(row)

    def verb_submit(self, presentation_id: int, answer: str, elapsed_ms: int) -> dict:
        """Save the first attempt before HTTP and make repeat submission idempotent; e.g. a timeout loses no typed text."""

        if not isinstance(answer, str) or not answer.strip() or len(answer) > 2000:
            raise ValueError("Type a sentence of at most 2,000 characters")
        if type(elapsed_ms) is not int or elapsed_ms < 0:
            raise ValueError("Invalid answer duration")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(PRESENTATION + " WHERE p.id=?", (presentation_id,)).fetchone()
            if row is None or row["shown_at"] is None:
                raise ValueError("This exercise has not been shown")
            if row["answer_text"] is not None or row["outcome"] is not None:
                return self._verb_row(row)
            if row["status"] != "active":
                raise ValueError("This verb round has ended")
            conn.execute("UPDATE verb_presentations SET answer_text=?,answered_at=?,elapsed_ms=? WHERE id=?",
                         (answer, time.time(), elapsed_ms, presentation_id))
            return self._verb_row(conn.execute(PRESENTATION + " WHERE p.id=?", (presentation_id,)).fetchone())

    @staticmethod
    def _verb_state(conn, target_id: int, result: dict) -> None:
        """Update only the derived schedule; e.g. immutable logs remain available for rebuilding."""

        conn.execute("INSERT INTO verb_states VALUES(?,?,?) ON CONFLICT(target_id) DO UPDATE "
                     "SET card_json=excluded.card_json,due_at=excluded.due_at",
                     (target_id, result["after_json"], result["due_at"]))

    @staticmethod
    def _verb_complete(conn) -> None:
        """Complete resolved rounds transactionally; e.g. closing the last feedback card cannot leave an active round."""

        conn.execute("UPDATE verb_sessions SET status='completed',finished_at=? WHERE status='active' "
                     "AND NOT EXISTS(SELECT 1 FROM verb_presentations p WHERE p.session_id=verb_sessions.id "
                     "AND p.outcome IS NULL)", (time.time(),))

    def verb_grade(self, presentation_id: int, grade: verbs.Grade, provenance: dict) -> dict:
        """Commit one verified correction, its error evidence and FSRS update together; e.g. double callbacks cannot add reviews."""

        from yt_whisper_subs import practice

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(PRESENTATION + " WHERE p.id=?", (presentation_id,)).fetchone()
            if row is None or row["answer_text"] is None:
                raise ValueError("This exercise has no submitted answer")
            if row["outcome"] is not None:
                return self._verb_row(row)
            if row["flagged_at"] is not None:
                raise ValueError("This question has been reported")
            if row["status"] != "active":
                raise ValueError("This verb round has ended")
            grade.check(row["answer_text"])
            state = conn.execute("SELECT card_json FROM verb_states WHERE target_id=?", (row["target_id"],)).fetchone()
            card = state[0] if state else practice.ReviewPolicy.new_card(row["target_id"], row["answered_at"])
            result = practice.ReviewPolicy(row["scheduler_json"]).answer(
                card, correct=grade.correct, unsure=False, now=row["answered_at"], elapsed_ms=row["elapsed_ms"])
            self._verb_state(conn, row["target_id"], result)
            conn.execute("UPDATE verb_presentations SET outcome=?,grade_json=?,grader_model=?,grader_reasoning=?,"
                         "grade_version=?,graded_at=?,rating=?,recall=?,due_at=?,before_json=?,after_json=?,log_json=? WHERE id=?",
                         ("correct" if grade.correct else "wrong", grade.model_dump_json(), provenance["model"],
                          provenance["reasoning"], provenance["grade_version"], time.time(), result["rating"],
                          result["recall"], result["due_at"], result["before_json"], result["after_json"], result["log_json"],
                          presentation_id))
            conn.executemany("INSERT INTO verb_assessments VALUES(?,?,?)",
                             [(presentation_id, item.axis, int(item.correct)) for item in grade.axes])
            conn.executemany("INSERT INTO verb_errors(presentation_id,ordinal,kind,axis,evidence,correction,explanation) "
                             "VALUES(?,?,?,?,?,?,?)", [(presentation_id, idx, item.kind, item.axis, item.evidence,
                                                        item.correction, item.explanation) for idx, item in enumerate(grade.errors)])
            self._verb_complete(conn)
            return self._verb_row(conn.execute(PRESENTATION + " WHERE p.id=?", (presentation_id,)).fetchone())

    def verb_flag(self, presentation_id: int) -> None:
        """Quarantine all uses and replay valid reviews; e.g. a faulty AI correction cannot keep punishing a tense."""

        from yt_whisper_subs import practice

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(PRESENTATION + " WHERE p.id=?", (presentation_id,)).fetchone()
            if row is None or row["shown_at"] is None:
                raise ValueError("This exercise has not been shown")
            conn.execute("UPDATE verb_questions SET flagged_at=? WHERE id=?", (time.time(), row["question_id"]))
            conn.execute("UPDATE verb_presentations SET outcome='invalid' WHERE question_id=?", (row["question_id"],))
            reviews = conn.execute(PRESENTATION + " WHERE q.target_id=? AND p.outcome IN ('correct','wrong') "
                                   "ORDER BY p.answered_at,p.id", (row["target_id"],)).fetchall()
            conn.execute("DELETE FROM verb_states WHERE target_id=?", (row["target_id"],))
            card = None
            for review in reviews:
                card = card or practice.ReviewPolicy.new_card(row["target_id"], review["answered_at"])
                result = practice.ReviewPolicy(review["scheduler_json"]).answer(
                    card, correct=review["outcome"] == "correct", unsure=False,
                    now=review["answered_at"], elapsed_ms=review["elapsed_ms"])
                card = result["after_json"]
                self._verb_state(conn, row["target_id"], result)
            self._verb_complete(conn)

    def verb_summary(self, session_id: str) -> dict:
        """Count actual exposure, submission and grading separately; e.g. pending corrections are never failures."""

        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS planned,COUNT(shown_at) AS shown,COUNT(answer_text) AS submitted,"
                               "COALESCE(SUM(outcome='correct'),0) AS correct,COALESCE(SUM(outcome='wrong'),0) AS wrong,"
                               "COALESCE(SUM(outcome='invalid'),0) AS invalid FROM verb_presentations WHERE session_id=?",
                               (session_id,)).fetchone()
        return dict(row)

    def verb_finish(self, session_id: str) -> dict:
        """End a round without grading unused exercises; e.g. partial effort remains visible in the calendar."""

        with self._connect() as conn:
            conn.execute("UPDATE verb_sessions SET status='stopped',finished_at=? WHERE id=? AND status='active'",
                         (time.time(), session_id))
        return self.verb_summary(session_id)
