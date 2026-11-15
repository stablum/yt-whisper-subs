"""Atomic sentence/word/form grading and rebuildable spaced-repetition caches.

Example: reporting a faulty bilingual pair removes all its uses while retaining the learner's original submissions.
"""

from __future__ import annotations

import json
import time


class SentenceReviewDbMixin:
    """Commit learning with its diagnostic evidence; e.g. a partial grade cannot change vocabulary due dates."""

    @staticmethod
    def _sentence_complete(conn) -> None:
        """Complete fully resolved sessions, including reports; e.g. closing final feedback leaves no unfinished round."""

        conn.execute("UPDATE sentence_sessions SET status='completed',finished_at=? WHERE status='active' "
                     "AND NOT EXISTS(SELECT 1 FROM sentence_presentations p WHERE p.session_id=sentence_sessions.id "
                     "AND p.outcome IS NULL)", (time.time(),))

    @staticmethod
    def _sentence_word_review(policy, state, row: dict, correct: bool) -> dict | None:
        """Avoid inflating known words during an early grammar retry; e.g. only a real failure overrides a future word due date."""

        from yt_whisper_subs import practice

        early = json.loads(row["options_json"])["include_early"]
        if state and state["due_at"] > row["answered_at"] and correct and not early:
            return None
        card_id = row["word_id"] * 2 + (row["direction"] == "en_nl")
        card = state["card_json"] if state else practice.ReviewPolicy.new_card(card_id, row["answered_at"])
        return policy.answer(card, correct=correct, unsure=False, now=row["answered_at"], elapsed_ms=row["elapsed_ms"])

    @staticmethod
    def _sentence_word_state(conn, row: dict, result: dict) -> None:
        """Store only derived word scheduling state; e.g. error analytics continue to use immutable assessments."""

        conn.execute("INSERT INTO sentence_states VALUES(?,?,?,?) ON CONFLICT(word_id,direction) DO UPDATE "
                     "SET card_json=excluded.card_json,due_at=excluded.due_at",
                     (row["word_id"], row["direction"], result["after_json"], result["due_at"]))

    def sentence_grade(self, presentation_id: int, grade, provenance: dict) -> dict:
        """Commit whole-answer correction and each target's independent review together; e.g. retries are idempotent."""

        from yt_whisper_subs import practice
        from yt_whisper_subs import sentence_db
        from yt_whisper_subs import vocabulary_learning

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            raw = conn.execute(sentence_db.PRESENTATION + " WHERE p.id=?", (presentation_id,)).fetchone()
            if raw is None or raw["answer_text"] is None:
                raise ValueError("This exercise has no submitted answer")
            if raw["outcome"] is not None:
                return self._sentence_row(raw)
            row = dict(raw)
            if row["flagged_at"] is not None:
                raise ValueError("This sentence exercise has been reported")
            content = json.loads(row["body_json"])
            grade.check(row["answer_text"], content)
            policy = practice.ReviewPolicy(row["scheduler_json"])
            self._sentence_assess(conn, row, grade, policy)
            previous = self._sentence_question_reviews(conn, row["question_id"], before=(row["answered_at"], row["id"]))
            card = previous["after_json"] if previous else policy.new_card(-row["question_id"], row["answered_at"])
            result = policy.answer(card, correct=grade.correct, unsure=False, now=row["answered_at"], elapsed_ms=row["elapsed_ms"])
            conn.execute("INSERT INTO sentence_question_states VALUES(?,?,?) ON CONFLICT(question_id) DO UPDATE "
                         "SET card_json=excluded.card_json,due_at=excluded.due_at",
                         (row["question_id"], result["after_json"], result["due_at"]))
            conn.execute("UPDATE sentence_presentations SET outcome=?,grade_json=?,grader_model=?,grader_reasoning=?,"
                         "grade_version=?,graded_at=?,rating=?,recall=?,due_at=?,before_json=?,after_json=?,log_json=? WHERE id=?",
                         ("correct" if grade.correct else "wrong", grade.model_dump_json(), provenance["model"], provenance["reasoning"],
                          provenance["grade_version"], time.time(), result["rating"], result["recall"], result["due_at"],
                          result["before_json"], result["after_json"], result["log_json"], presentation_id))
            completed = {**row, "content": content, "grade": grade.model_dump()}
            self._learning_save(conn, "sentence", completed, vocabulary_learning.Corrections(vocabulary=grade.vocabulary), provenance)
            lookups = {item["word_id"]: item["lookup_id"] for item in conn.execute(
                "SELECT l.word_id,w.lookup_id FROM sentence_question_words w JOIN vocab_lookups l ON l.id=w.lookup_id "
                "WHERE w.question_id=?", (row["question_id"],))}
            for word in grade.words:
                if word.target_used or not word.correct:
                    self._word_observe(conn, "sentence", row, lookups[word.word_id], word.correct)
                self._sentence_replay(conn, word.word_id, row["direction"])
            latest = self._sentence_question_reviews(conn, row["question_id"])
            conn.execute("UPDATE sentence_question_states SET card_json=?,due_at=? WHERE question_id=?",
                         (latest["after_json"], latest["due_at"], row["question_id"]))
            self._sentence_complete(conn)
            return self._sentence_row(conn.execute(sentence_db.PRESENTATION + " WHERE p.id=?", (presentation_id,)).fetchone())

    def _sentence_assess(self, conn, row: dict, grade, policy) -> None:
        """Persist lexical meaning, inflection and localized errors; e.g. a wrong supporting article does not fail unrelated vocabulary."""

        lookups = {item["word_id"]: item["lookup_id"] for item in conn.execute(
            "SELECT l.word_id,w.lookup_id FROM sentence_question_words w JOIN vocab_lookups l ON l.id=w.lookup_id "
            "WHERE w.question_id=?", (row["question_id"],))}
        keys = ("rating", "recall", "due_at", "before_json", "after_json", "log_json")
        for word in grade.words:
            state = self._sentence_replay(conn, word.word_id, row["direction"], before=(row["answered_at"], row["id"]))
            target = {**row, "word_id": word.word_id}
            result = self._sentence_word_review(policy, state, target, word.correct) if word.target_used or not word.correct else None
            values = [result[key] if result else None for key in keys]
            lookup_id = lookups[word.word_id]
            conn.execute("INSERT INTO sentence_assessments VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (row["id"], lookup_id, int(word.meaning_correct), int(word.target_used), word.form_correct,
                          word.explanation, int(result is not None), *values))
            if result:
                self._sentence_word_state(conn, target, result)
            conn.executemany("INSERT INTO sentence_forms VALUES(?,?,?,?,?)",
                             [(row["id"], lookup_id, item.feature, item.correct, item.explanation) for item in word.forms])
        for idx, error in enumerate(grade.errors):
            cursor = conn.execute("INSERT INTO sentence_errors(presentation_id,ordinal,kind,evidence,correction,explanation) "
                                  "VALUES(?,?,?,?,?,?)", (row["id"], idx, error.kind, error.evidence, error.correction, error.explanation))
            conn.executemany("INSERT INTO sentence_error_words VALUES(?,?)",
                             [(cursor.lastrowid, lookups[word_id]) for word_id in error.word_ids])

    def sentence_flag(self, presentation_id: int) -> None:
        """Quarantine every use of an exercise and replay remaining word observations; e.g. disputed AI grading cannot bias later selection."""

        from yt_whisper_subs import sentence_db

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(sentence_db.PRESENTATION + " WHERE p.id=?", (presentation_id,)).fetchone()
            if row is None or row["shown_at"] is None:
                raise ValueError("This sentence has not been presented")
            conn.execute("UPDATE sentence_questions SET flagged_at=? WHERE id=?", (time.time(), row["question_id"]))
            conn.execute("UPDATE sentence_presentations SET outcome='invalid' WHERE question_id=?", (row["question_id"],))
            self._word_unlink(conn, "sentence", row["question_id"])
            conn.execute("DELETE FROM sentence_question_states WHERE question_id=?", (row["question_id"],))
            words = conn.execute("SELECT l.word_id FROM sentence_question_words w JOIN vocab_lookups l ON l.id=w.lookup_id "
                                 "WHERE w.question_id=?", (row["question_id"],)).fetchall()
            for word in words:
                self._sentence_replay(conn, word[0], row["direction"])
            self._sentence_complete(conn)

    def _sentence_replay(self, conn, word_id: int, direction: str, *, before: tuple | None = None) -> dict | None:
        """Replay submission order, optionally only its prefix; e.g. retrying an older failed correction cannot move learning backwards."""

        from yt_whisper_subs import practice

        rows = conn.execute("SELECT p.*,s.direction,s.options_json,s.scheduler_json,l.word_id,a.meaning_correct,a.form_correct,a.target_used "
                            "FROM sentence_assessments a JOIN sentence_presentations p ON p.id=a.presentation_id "
                            "JOIN sentence_sessions s ON s.id=p.session_id JOIN vocab_lookups l ON l.id=a.lookup_id "
                            "WHERE l.word_id=? AND s.direction=? AND p.outcome IN ('correct','wrong') "
                            "ORDER BY p.answered_at,p.id", (word_id, direction)).fetchall()
        conn.execute("DELETE FROM sentence_states WHERE word_id=? AND direction=?", (word_id, direction))
        state = None
        for raw in rows:
            row = dict(raw)
            if before is not None and (row["answered_at"], row["id"]) >= before:
                break
            correct = bool(row["meaning_correct"]) and row["form_correct"] != 0
            if correct and not row["target_used"]:
                continue
            result = self._sentence_word_review(practice.ReviewPolicy(row["scheduler_json"]), state, row, correct)
            if result:
                self._sentence_word_state(conn, row, result)
                state = {"card_json": result["after_json"], "due_at": result["due_at"]}
        return state

    @staticmethod
    def _sentence_question_reviews(conn, question_id: int, *, before: tuple | None = None) -> dict | None:
        """Reconstruct a sentence's chronological skill state; e.g. corrections from stopped rounds may arrive after later answers."""

        from yt_whisper_subs import practice

        rows = conn.execute("SELECT p.*,s.scheduler_json FROM sentence_presentations p JOIN sentence_sessions s ON s.id=p.session_id "
                            "WHERE p.question_id=? AND p.outcome IN ('correct','wrong') ORDER BY p.answered_at,p.id", (question_id,))
        result = None
        for row in rows:
            if before is not None and (row["answered_at"], row["id"]) >= before:
                break
            policy = practice.ReviewPolicy(row["scheduler_json"])
            card = result["after_json"] if result else policy.new_card(-question_id, row["answered_at"])
            result = policy.answer(card, correct=row["outcome"] == "correct", unsure=False,
                                   now=row["answered_at"], elapsed_ms=row["elapsed_ms"])
        return result
