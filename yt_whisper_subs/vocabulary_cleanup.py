"""Quarantine reviewed vocabulary errors while retaining restorable source evidence.

Example: a bogus subtitle word disappears from practice without deleting answered exercises.
"""

import json
import time

from yt_whisper_subs import vocabulary


SCHEMA = """
CREATE TABLE IF NOT EXISTS vocab_exclusions (
    language TEXT NOT NULL, lemma_norm TEXT NOT NULL, reason TEXT NOT NULL,
    evidence_json TEXT NOT NULL, archived_json TEXT NOT NULL, removed_at REAL NOT NULL,
    PRIMARY KEY(language,lemma_norm)
);
CREATE TABLE IF NOT EXISTS vocab_retained_components (
    lookup_id INTEGER PRIMARY KEY REFERENCES vocab_lookups(id),
    parent TEXT NOT NULL, reason TEXT NOT NULL, retained_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS vocab_repairs (
    language TEXT NOT NULL, old_lemma TEXT NOT NULL, new_lemma TEXT NOT NULL,
    reason TEXT NOT NULL, before_json TEXT NOT NULL, repaired_at REAL NOT NULL,
    PRIMARY KEY(language,old_lemma)
);
"""

# Queries using this predicate bind w to vocab_words and l to vocab_lookups.
ELIGIBLE = """NOT EXISTS(SELECT 1 FROM vocab_exclusions x WHERE x.language=w.language
                       AND x.lemma_norm IN (w.normalized,l.lemma_norm))"""

OCCURRENCES = """
SELECT 'retained:'||r.lookup_id AS event_key,'component' AS origin,r.lookup_id,l.lemma AS surface,
       r.retained_at AS occurred_at,NULL AS source_id,NULL AS source_key,NULL AS video_id,
       'Reviewed word from '||r.parent AS title,'' AS url,'' AS path,NULL AS position_seconds,
       NULL AS cue_start_ms,NULL AS cue_end_ms,NULL AS token_start,NULL AS token_end,
       '' AS typed,r.parent AS correction,'component' AS role
FROM vocab_retained_components r JOIN vocab_lookups l ON l.id=r.lookup_id
"""


class CleanupDbMixin:
    """Keep deliberate removal separate from linguistic uncertainty; e.g. rare but valid compounds remain untouched."""

    @staticmethod
    def _word_allowed(conn, language: str, lemma: str) -> bool:
        """Prevent automatic reimport of a reviewed error; e.g. a cached compound cannot resurrect a removed nonword."""

        norm = vocabulary.normalized(lemma.strip())
        return conn.execute("SELECT 1 FROM vocab_exclusions WHERE language=? AND lemma_norm=?", (language, norm)).fetchone() is None

    def remove_vocabulary(self, decisions: list[dict]) -> list[str]:
        """Archive exact source links and retire cached exercises atomically; e.g. original answers and review logs survive cleanup."""

        removed, now = [], time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for decision in decisions:
                language, lemma = decision["language"], vocabulary.normalized(decision["lemma"])
                if not decision["reason"].strip() or not decision["evidence"]:
                    raise ValueError("Removal needs reviewed contextual evidence")
                if not self._word_allowed(conn, language, lemma):
                    continue
                ids = [row[0] for row in conn.execute(
                    "SELECT l.id FROM vocab_lookups l JOIN vocab_words w ON w.id=l.word_id "
                    "WHERE w.language=? AND (w.normalized=? OR l.lemma_norm=?)", (language, lemma, lemma))]
                if not ids:
                    raise ValueError(f"Unknown vocabulary identity: {lemma}")
                key = json.dumps(ids)
                keep = decision.get("keep_components", [])
                components = conn.execute(
                    "SELECT d.lookup_id,d.parent,l.lemma_norm FROM vocab_additions d JOIN vocab_audit_sources a ON a.id=d.source_id "
                    "JOIN vocab_lookups l ON l.id=d.lookup_id WHERE d.role='component' "
                    "AND a.lookup_id IN (SELECT value FROM json_each(?))", (key,)).fetchall()
                for word in components:
                    if word["lemma_norm"] in keep:
                        conn.execute("INSERT OR IGNORE INTO vocab_retained_components VALUES(?,?,?,?)",
                                     (word["lookup_id"], word["parent"], "Independently reviewed valid word; parent entry removed.", now))
                archived = {}
                for table in ("vocab_clicks", "vocab_error_words", "vocab_additions", "verb_vocab_links"):
                    archived[table] = [dict(row) for row in conn.execute(
                        f"SELECT * FROM {table} WHERE lookup_id IN (SELECT value FROM json_each(?))", (key,))]
                    conn.execute(f"DELETE FROM {table} WHERE lookup_id IN (SELECT value FROM json_each(?))", (key,))
                conn.execute("INSERT INTO vocab_exclusions VALUES(?,?,?,?,?,?)",
                             (language, lemma, decision["reason"], json.dumps(decision["evidence"], ensure_ascii=False),
                              json.dumps(archived, ensure_ascii=False), now))
                self._retire_word_questions(conn, key, lemma)
                removed.append(lemma)
            for kind in ("practice", "sentence", "verb"):
                conn.execute(f"UPDATE {kind}_presentations SET outcome='invalid' WHERE outcome IS NULL AND question_id IN ("
                             f"SELECT id FROM {kind}_questions WHERE flagged_at IS NOT NULL)")
            self._practice_complete(conn)
            self._sentence_complete(conn)
            self._verb_complete(conn)
        return removed

    @staticmethod
    def _retire_word_questions(conn, key: str, lemma: str) -> None:
        """Retire cached teaching material while retaining answered snapshots; e.g. an excluded target cannot remain in standby stock."""

        now = time.time()
        conn.execute("UPDATE practice_questions SET flagged_at=COALESCE(flagged_at,?) "
                     "WHERE lookup_id IN (SELECT value FROM json_each(?))", (now, key))
        conn.execute("UPDATE sentence_questions SET flagged_at=COALESCE(flagged_at,?) WHERE id IN ("
                     "SELECT question_id FROM sentence_question_words WHERE lookup_id IN (SELECT value FROM json_each(?)))", (now, key))
        conn.execute("UPDATE verb_questions SET flagged_at=COALESCE(flagged_at,?) WHERE target_id IN ("
                     "SELECT t.id FROM verb_targets t JOIN verb_lemmas v ON v.id=t.verb_id WHERE v.lemma=?)", (now, lemma))

    def repair_vocabulary(self, repairs: list[dict]) -> list[dict]:
        """Correct established spellings in place, restoring any quarantine first; e.g. eindoordeel keeps its original word review."""

        repaired = []
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for repair in repairs:
                language, old = repair["language"], vocabulary.normalized(repair["old"])
                new = vocabulary.normalized(repair["new"])
                if conn.execute("SELECT 1 FROM vocab_repairs WHERE language=? AND old_lemma=?", (language, old)).fetchone():
                    continue
                archive = conn.execute("SELECT archived_json FROM vocab_exclusions WHERE language=? AND lemma_norm=?", (language, old)).fetchone()
                if archive:
                    for table, rows in json.loads(archive[0]).items():
                        if table not in {"vocab_clicks", "vocab_error_words", "vocab_additions", "verb_vocab_links"}:
                            continue
                        for row in rows:
                            columns = ",".join(row)
                            slots = ",".join("?" for _ in row)
                            conn.execute(f"INSERT OR IGNORE INTO {table}({columns}) VALUES({slots})", tuple(row.values()))
                rows = [dict(row) for row in conn.execute(
                    "SELECT l.* FROM vocab_lookups l JOIN vocab_words w ON w.id=l.word_id "
                    "WHERE w.language=? AND (w.normalized=? OR l.lemma_norm=?)", (language, old, old))]
                if not rows:
                    raise ValueError(f"Unknown vocabulary identity: {old}")
                conn.execute("DELETE FROM vocab_exclusions WHERE language=? AND lemma_norm=?", (language, old))
                for row in rows:
                    self._repair_lookup(conn, row, repair)
                conn.execute("INSERT INTO vocab_repairs VALUES(?,?,?,?,?,?)",
                             (language, old, new, repair["reason"], json.dumps(dict(lookups=rows, quarantine=archive[0] if archive else None),
                                                                           ensure_ascii=False), time.time()))
                repaired.append(dict(old=old, new=new, lookup_ids=[row["id"] for row in rows]))
        return repaired

    def _repair_lookup(self, conn, row: dict, repair: dict) -> None:
        """Update teaching text while retaining IDs and raw evidence in the repair journal; e.g. a corrected inflection gets an exact new span."""

        context = json.loads(row["context_json"])
        surfaces = repair.get("surfaces", {})
        form = surfaces.get(context["word"], context["word"])
        if context.get("span"):
            start, end = context["span"]
            context["sentence"] = context["sentence"][:start] + form + context["sentence"][end:]
            context["span"] = [start, start + len(form)]
        context["word"] = form
        note = f"Reviewed spelling correction: {repair['old']} → {repair['new']}."
        conn.execute("UPDATE vocab_lookups SET lemma=?,lemma_norm=?,lemma_meaning=?,meaning=?,ambiguous=0,context_json=?,note=?,updated_at=? WHERE id=?",
                     (repair["new"], vocabulary.normalized(repair["new"]), repair.get("meaning", row["lemma_meaning"]),
                      repair.get("context_meaning", row["meaning"]), json.dumps(context, ensure_ascii=False), note, time.time(), row["id"]))
        for old, new in surfaces.items():
            conn.execute("UPDATE vocab_clicks SET surface=? WHERE lookup_id=? AND surface=?", (new, row["id"], old))
            norm = vocabulary.normalized(new)
            if not conn.execute("SELECT 1 FROM vocab_words WHERE language=? AND normalized=? AND id<>?", (repair["language"], norm, row["word_id"])).fetchone():
                conn.execute("UPDATE vocab_words SET normalized=? WHERE id=? AND normalized=?", (norm, row["word_id"], vocabulary.normalized(old)))
        conn.execute("DELETE FROM vocab_additions WHERE source_id IN (SELECT id FROM vocab_audit_sources WHERE lookup_id=?)", (row["id"],))
        conn.execute("DELETE FROM vocab_audit_sources WHERE lookup_id=?", (row["id"],))
        self._accretion_compound(conn, row["id"])
