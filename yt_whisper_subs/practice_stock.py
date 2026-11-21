"""Relational, ordered standby rounds independent from active practice history.

Example: preparing the next round cannot stop the current one or count questions as presented.
"""

import json
import time

from yt_whisper_subs import practice_tracks


SCHEMA = "\n".join(f"""
CREATE TABLE IF NOT EXISTS {kind}_stock (
    direction TEXT PRIMARY KEY,
    options_json TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS {kind}_stock_questions (
    direction TEXT NOT NULL REFERENCES {kind}_stock(direction) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL CHECK(ordinal>=1),
    question_id INTEGER NOT NULL REFERENCES {kind}_questions(id),
    display_json TEXT NOT NULL,
    PRIMARY KEY(direction,ordinal),
    UNIQUE(direction,question_id)
);
""" for kind in practice_tracks.QUESTION_KINDS)


class StockDbMixin:
    """Keep one bounded spare per destination; e.g. unused approvals survive option changes and remain reusable question stock."""

    def practice_reserved(self, kind: str, opts) -> set[int]:
        """Exclude questions already assigned to an active round from background stock; e.g. its unseen last question is not also next round's first."""

        track = practice_tracks.Track.for_options(kind, opts)
        scope = track.session_filter
        where = f" AND s.{scope[0]}=?" if scope else ""
        params = (scope[1],) if scope else ()
        with self._connect() as conn:
            return {row[0] for row in conn.execute(f"SELECT p.question_id FROM {kind}_presentations p "
                    f"JOIN {kind}_sessions s ON s.id=p.session_id WHERE s.status='active'{where}", params)}

    def practice_spare_candidates(self, kind: str, opts, rows: list[dict]) -> list[dict]:
        """Prefer different targets for the next round when enough are eligible; e.g. preparing while typing cannot immediately repeat the current ten anchors."""

        track = practice_tracks.Track.for_options(kind, opts)
        value = {"practice": "l.word_id", "word_order": "l.word_id", "sentence": "q.anchor_word_id", "verb": "q.target_id"}[kind]
        extra = " JOIN vocab_lookups l ON l.id=q.lookup_id" if kind in {"practice", "word_order"} else ""
        scope = track.session_filter
        where = f" AND s.{scope[0]}=?" if scope else ""
        params = (scope[1],) if scope else ()
        with self._connect() as conn:
            active = {row[0] for row in conn.execute(f"SELECT {value} FROM {kind}_presentations p "
                      f"JOIN {kind}_questions q ON q.id=p.question_id JOIN {kind}_sessions s ON s.id=p.session_id{extra} "
                      f"WHERE s.status='active'{where}", params)}
        key = "target_id" if kind == "verb" else "word_id"
        fresh = [row for row in rows if row.get(key) not in active]
        now = time.time()
        eligible = sum(not row["card_json"] or row["due_at"] <= now or opts.include_early for row in fresh)
        return fresh if eligible >= opts.count else rows

    def practice_stage(self, kind: str, opts, questions: list[dict]) -> str:
        """Stage complete checked content without sessions or exposures; e.g. pending sentence corrections can finish while the next round is ready."""

        track = practice_tracks.Track.for_options(kind, opts)
        direction = track.stock_key
        if len(questions) != opts.count or len({row["id"] for row in questions}) != opts.count:
            raise ValueError("A standby round needs its full requested number of distinct questions")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(f"DELETE FROM {kind}_stock WHERE direction=?", (direction,))
            conn.execute(f"INSERT INTO {kind}_stock VALUES(?,?,?)", (direction, opts.model_dump_json(), time.time()))
            displays = [json.dumps({key: row[key] for key in ("choices", "correct_idx")} if kind == "practice" else {}) for row in questions]
            conn.executemany(f"INSERT INTO {kind}_stock_questions VALUES(?,?,?,?)",
                             [(direction, idx, row["id"], display) for idx, (row, display) in enumerate(zip(questions, displays), 1)])
        return track.value

    def practice_stock(self, track: practice_tracks.Track, opts, policy: dict) -> list[dict]:
        """Read matching, still-approved questions in stored order; e.g. a disputed question cannot remain playable in a ready round."""

        kind = track.kind
        direction = track.stock_key
        scope = track.session_filter
        with self._connect() as conn:
            row = conn.execute(f"SELECT options_json FROM {kind}_stock WHERE direction=?", (direction,)).fetchone()
            if row is None or json.loads(row[0]) != opts.model_dump(mode="json"):
                return []
            rows = conn.execute(f"SELECT q.*,p.display_json FROM {kind}_stock_questions p JOIN {kind}_questions q ON q.id=p.question_id "
                                "WHERE p.direction=? ORDER BY p.ordinal", (direction,)).fetchall()
            valid = len(rows) == opts.count and all(row["flagged_at"] is None and (not scope or row[scope[0]] == scope[1])
                                                   and all(row[key] == value for key, value in policy.items()) for row in rows)
            return [{**dict(row), **json.loads(row["display_json"])} for row in rows] if valid else []

    @staticmethod
    def _practice_consume_stock(conn, kind: str, direction: str = "") -> None:
        """Consume a standby round in the session creation transaction; e.g. a crash cannot play the same spare twice."""

        conn.execute(f"DELETE FROM {kind}_stock WHERE direction=?", (direction,))
