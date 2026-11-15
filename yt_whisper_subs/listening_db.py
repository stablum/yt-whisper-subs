"""Relate listening recordings and replay events to the existing sentence history.

Example: replaying slowly adds an assistance event, never another submitted answer.
"""

import time

from yt_whisper_subs import listening_tts


SCHEMA = """
CREATE TABLE IF NOT EXISTS listening_assets (
    cache_key TEXT PRIMARY KEY,
    voice_profile TEXT NOT NULL,
    normal_sha256 TEXT NOT NULL,
    slow_sha256 TEXT NOT NULL,
    normal_seconds REAL NOT NULL CHECK(normal_seconds>0),
    slow_seconds REAL NOT NULL CHECK(slow_seconds>0),
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS listening_audio (
    question_id INTEGER PRIMARY KEY REFERENCES sentence_questions(id),
    cache_key TEXT NOT NULL REFERENCES listening_assets(cache_key)
);
CREATE TABLE IF NOT EXISTS listening_plays (
    id INTEGER PRIMARY KEY,
    presentation_id INTEGER NOT NULL REFERENCES sentence_presentations(id),
    played_at REAL NOT NULL,
    speed TEXT NOT NULL CHECK(speed IN ('normal','slow')),
    before_answer INTEGER NOT NULL CHECK(before_answer IN (0,1)),
    cache_key TEXT NOT NULL REFERENCES listening_assets(cache_key)
);
CREATE INDEX IF NOT EXISTS idx_listening_plays ON listening_plays(presentation_id,played_at);
"""


class ListeningDbMixin:
    """Keep audio provenance separate from answer grading; e.g. changing voices cannot rewrite learning history."""

    def listening_paths(self, key: str) -> dict:
        """Derive opaque recording paths from a trusted digest; e.g. filenames do not disclose the hidden sentence."""

        if len(key) != 64 or any(char not in "0123456789abcdef" for char in key):
            raise ValueError("Invalid listening cache key")
        root = self.path.parent / "practice-audio" / "sentences"
        return {speed: root / f"{key}-{speed}.wav" for speed in ("normal", "slow")}

    def listening_audio(self, question_id: int) -> dict | None:
        """Read durable speech metadata without revealing it in the UI; e.g. a replay needs no synthesis."""

        with self._connect() as conn:
            row = conn.execute("SELECT q.question_id,a.* FROM listening_audio q JOIN listening_assets a USING(cache_key) "
                               "WHERE q.question_id=?", (question_id,)).fetchone()
        return dict(row) if row else None

    def listening_audio_valid(self, row: dict) -> bool:
        """Check each published recording before declaring stock ready; e.g. a deleted or corrupt file gets rebuilt."""

        try:
            return all(path.is_file() and listening_tts.digest(path) == row[f"{speed}_sha256"]
                       for speed, path in self.listening_paths(row["cache_key"]).items())
        except OSError:
            return False

    def listening_store(self, question_id: int, key: str, profile: str, paths: dict) -> None:
        """Publish metadata only after both files validate; e.g. an interrupted render cannot become ready audio."""

        values = [listening_tts.digest(paths[speed]) for speed in ("normal", "slow")]
        seconds = [listening_tts.duration(paths[speed]) for speed in ("normal", "slow")]
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO listening_assets VALUES(?,?,?,?,?,?,?) ON CONFLICT(cache_key) DO UPDATE SET "
                         "normal_sha256=excluded.normal_sha256,slow_sha256=excluded.slow_sha256,"
                         "normal_seconds=excluded.normal_seconds,slow_seconds=excluded.slow_seconds",
                         (key, profile, *values, *seconds, time.time()))
            conn.execute("INSERT INTO listening_audio VALUES(?,?) ON CONFLICT(question_id) DO UPDATE SET cache_key=excluded.cache_key",
                         (question_id, key))

    def listening_play(self, presentation_id: int, speed: str) -> None:
        """Record only successfully started playback of a visible question; e.g. a post-correction replay is not pre-answer assistance."""

        with self._connect() as conn:
            row = conn.execute("SELECT p.answer_text,a.cache_key FROM sentence_presentations p JOIN sentence_questions q ON q.id=p.question_id "
                               "JOIN listening_audio a ON a.question_id=q.id WHERE p.id=? AND p.shown_at IS NOT NULL "
                               "AND q.direction='listen_nl'", (presentation_id,)).fetchone()
            if row is None:
                raise ValueError("This listening exercise has not been opened")
            conn.execute("INSERT INTO listening_plays(presentation_id,played_at,speed,before_answer,cache_key) VALUES(?,?,?,?,?)",
                         (presentation_id, time.time(), speed, int(row["answer_text"] is None), row["cache_key"]))
