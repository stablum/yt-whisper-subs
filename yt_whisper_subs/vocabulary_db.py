"""Normalized vocabulary storage composed into the existing SQLite catalog.

Example: one contextual lookup can serve many independently timestamped clicks.
"""

from __future__ import annotations

import json
import time

from yt_whisper_subs import vocabulary as words


SCHEMA = """
CREATE TABLE IF NOT EXISTS vocab_sources (
    id INTEGER PRIMARY KEY,
    source_key TEXT NOT NULL UNIQUE,
    video_id TEXT,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    path TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vocab_words (
    id INTEGER PRIMARY KEY,
    language TEXT NOT NULL,
    normalized TEXT NOT NULL,
    UNIQUE(language, normalized)
);
CREATE TABLE IF NOT EXISTS vocab_lookups (
    id INTEGER PRIMARY KEY,
    cache_key TEXT NOT NULL UNIQUE,
    word_id INTEGER NOT NULL REFERENCES vocab_words(id),
    context_json TEXT NOT NULL,
    model TEXT NOT NULL,
    reasoning TEXT NOT NULL,
    prompt_version INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','working','ready','failed')),
    meaning TEXT,
    lemma TEXT,
    lemma_norm TEXT,
    lemma_meaning TEXT,
    part_of_speech TEXT,
    note TEXT,
    expression TEXT,
    ambiguous INTEGER CHECK(ambiguous IN (0,1)),
    error TEXT,
    lease_owner TEXT,
    lease_until REAL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS vocab_clicks (
    id INTEGER PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    source_id INTEGER NOT NULL REFERENCES vocab_sources(id),
    lookup_id INTEGER NOT NULL REFERENCES vocab_lookups(id),
    surface TEXT NOT NULL,
    clicked_at REAL NOT NULL,
    position_seconds REAL NOT NULL CHECK(position_seconds >= 0),
    cue_start_ms INTEGER NOT NULL CHECK(cue_start_ms >= 0),
    cue_end_ms INTEGER NOT NULL CHECK(cue_end_ms > cue_start_ms),
    token_start INTEGER NOT NULL CHECK(token_start >= 0),
    token_end INTEGER NOT NULL CHECK(token_end > token_start)
);
CREATE INDEX IF NOT EXISTS idx_vocab_clicks_lookup ON vocab_clicks(lookup_id);
CREATE INDEX IF NOT EXISTS idx_vocab_clicks_source ON vocab_clicks(source_id, cue_start_ms);
CREATE INDEX IF NOT EXISTS idx_vocab_clicks_date ON vocab_clicks(clicked_at DESC);
CREATE INDEX IF NOT EXISTS idx_vocab_pending ON vocab_lookups(state, lease_until, id);
CREATE INDEX IF NOT EXISTS idx_vocab_lookup_word ON vocab_lookups(word_id);
"""

_EVENTS = """
SELECT c.*, s.source_key, s.video_id, s.title, s.url, s.path,
       w.language, w.normalized, l.state, l.meaning, l.lemma, l.lemma_norm,
       l.lemma_meaning, l.part_of_speech, l.note, l.expression, l.ambiguous,
       l.error, l.context_json
FROM vocab_clicks c
JOIN vocab_sources s ON s.id=c.source_id
JOIN vocab_lookups l ON l.id=c.lookup_id
JOIN vocab_words w ON w.id=l.word_id
"""


class VocabularyDbMixin:
    """Keep vocabulary transactions independent from channel queries; e.g. CLI capture."""

    def has_unfinished_word_lookups(self) -> bool:
        """Avoid idle background workers for an empty vocabulary; e.g. initial library startup."""

        with self._connect() as conn:
            return conn.execute("SELECT 1 FROM vocab_lookups WHERE state IN ('pending','working') LIMIT 1").fetchone() is not None

    def save_word(self, selection: words.Selection, lookup: words.LookupIdentity) -> dict:
        """Commit a click before acknowledging it; e.g. replayed transport IDs are idempotent."""

        source, ctx = selection.source, selection.context
        cache_key, model, version, reasoning = lookup
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO vocab_sources(source_key,video_id,title,url,path) VALUES(?,?,?,?,?) "
                         "ON CONFLICT(source_key) DO UPDATE SET title=excluded.title, "
                         "url=excluded.url,path=excluded.path", source)
            source_id = conn.execute("SELECT id FROM vocab_sources WHERE source_key=?", (source.key,)).fetchone()[0]
            conn.execute("INSERT OR IGNORE INTO vocab_words(language,normalized) VALUES(?,?)",
                         (ctx.language, words.normalized(ctx.surface)))
            word_id = conn.execute("SELECT id FROM vocab_words WHERE language=? AND normalized=?",
                                   (ctx.language, words.normalized(ctx.surface))).fetchone()[0]
            conn.execute("INSERT OR IGNORE INTO vocab_lookups "
                         "(cache_key,word_id,context_json,model,prompt_version,reasoning,updated_at) VALUES(?,?,?,?,?,?,?)",
                         (cache_key, word_id, json.dumps(ctx.document(), ensure_ascii=False), model, version, reasoning, time.time()))
            lookup_id = conn.execute("SELECT id FROM vocab_lookups WHERE cache_key=?", (cache_key,)).fetchone()[0]
            conn.execute("INSERT OR IGNORE INTO vocab_clicks "
                         "(event_id,source_id,lookup_id,surface,clicked_at,position_seconds,"
                         "cue_start_ms,cue_end_ms,token_start,token_end) VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (selection.event_id, source_id, lookup_id, ctx.surface, selection.clicked_at,
                          selection.position, ctx.cue.start_ms, ctx.cue.end_ms, ctx.token.start, ctx.token.end))
            result = dict(conn.execute("SELECT * FROM vocab_lookups WHERE id=?", (lookup_id,)).fetchone())
            counts = conn.execute("SELECT COUNT(*), COUNT(DISTINCT c.source_id) FROM vocab_clicks c "
                                  "JOIN vocab_lookups l ON l.id=c.lookup_id WHERE l.word_id=?", (word_id,)).fetchone()
            result.update(clicks=counts[0], videos=counts[1])
        return result

    def word_marks(self, source_key: str) -> set[tuple[str, int, str, int]]:
        """Restore saved underlines by text and span; e.g. regenerated cues never inherit a wrong mark."""

        with self._connect() as conn:
            rows = conn.execute("SELECT c.cue_start_ms,l.context_json,c.token_start FROM vocab_clicks c "
                                "JOIN vocab_sources s ON s.id=c.source_id "
                                "JOIN vocab_lookups l ON l.id=c.lookup_id WHERE s.source_key=?", (source_key,))
            marks = set()
            for row in rows:
                ctx = json.loads(row[1])
                marks.add((ctx["language"], row[0], ctx["sentence"], row[2]))
            return marks

    def claim_word_lookup(self, owner: str) -> dict | None:
        """Lease one durable request across processes; e.g. GUI and CLI cannot duplicate a lookup."""

        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM vocab_lookups WHERE state='pending' "
                               "OR (state='working' AND lease_until < ?) ORDER BY id LIMIT 1", (now,)).fetchone()
            if row is None:
                return None
            # The lease token is separate from the deadline, protecting late completions.
            conn.execute("UPDATE vocab_lookups SET state='working',lease_until=?,lease_owner=?,updated_at=? WHERE id=?",
                         (now + 120, owner, now, row["id"]))
            return dict(row)

    def finish_word_lookup(self, lookup_id: int, owner: str, result: words.Meaning | str) -> None:
        """Complete only our own lease; e.g. a timed-out old worker cannot overwrite a retry."""

        with self._connect() as conn:
            if isinstance(result, str):
                conn.execute("UPDATE vocab_lookups SET state='failed',error=?,lease_until=NULL,lease_owner=NULL,updated_at=? "
                             "WHERE id=? AND state='working' AND lease_owner=?", (result, time.time(), lookup_id, owner))
            else:
                conn.execute("UPDATE vocab_lookups SET state='ready',meaning=?,lemma=?,lemma_norm=?,"
                             "lemma_meaning=?,part_of_speech=?,note=?,expression=?,ambiguous=?,"
                             "error=NULL,lease_until=NULL,lease_owner=NULL,updated_at=? WHERE id=? AND state='working' AND lease_owner=?",
                             (result.meaning, result.lemma, words.normalized(result.lemma), result.lemma_meaning,
                              result.part_of_speech, result.note, result.expression, int(result.ambiguous),
                              time.time(), lookup_id, owner))

    def word_lookup(self, lookup_id: int) -> dict:
        """Read translation state for acknowledgements; e.g. another process completed it."""

        with self._connect() as conn:
            return dict(conn.execute("SELECT * FROM vocab_lookups WHERE id=?", (lookup_id,)).fetchone())

    def retry_word_lookups(self) -> int:
        """Retry failed translations without adding clicks; e.g. after fixing an API key."""

        with self._connect() as conn:
            return conn.execute("UPDATE vocab_lookups SET state='pending',error=NULL WHERE state='failed'").rowcount

    def word_history(self, language: str, key: str, *, by_lemma: bool = False) -> list[dict]:
        """List every captured occurrence; e.g. repeated clicks in the same scene remain separate."""

        group = "COALESCE(NULLIF(lemma_norm,''),normalized)" if by_lemma else "normalized"
        with self._connect() as conn:
            rows = conn.execute(f"WITH events AS ({_EVENTS}) SELECT * FROM events "
                                f"WHERE language=? AND {group}=? ORDER BY clicked_at DESC,id DESC", (language, key))
            return [dict(row) for row in rows]

    def word_summary(self, *, by_lemma: bool = False) -> list[dict]:
        """Derive counts from history rather than mutable counters; e.g. count distinct videos."""

        group = "COALESCE(NULLIF(lemma_norm,''),normalized)" if by_lemma else "normalized"
        sql = f"""
            WITH events AS ({_EVENTS}), grouped AS (
                SELECT *, {group} AS word_key FROM events
            ), counts AS (
                SELECT language,word_key,COUNT(*) AS clicks,COUNT(DISTINCT source_id) AS videos,
                       SUM(state='failed') AS failed,SUM(state IN ('pending','working')) AS pending
                FROM grouped GROUP BY language,word_key
            ), recent AS (
                SELECT *,ROW_NUMBER() OVER(PARTITION BY language,word_key ORDER BY clicked_at DESC,id DESC) AS n
                FROM grouped
            ) SELECT r.*,c.clicks,c.videos,c.failed,c.pending FROM recent r
              JOIN counts c USING(language,word_key) WHERE n=1 ORDER BY clicked_at DESC,id DESC
        """
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(sql)]
