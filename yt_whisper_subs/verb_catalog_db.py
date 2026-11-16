"""Link contextual vocabulary occurrences to a shared, deduplicated verb lexicon.

Example: liep and gelopen both reference lopen, preserving one review history and their source lookups.
"""

from __future__ import annotations

import json
import time

from yt_whisper_subs import vocabulary_db
from yt_whisper_subs import word_reviews


class VerbCatalogDbMixin:
    """Own lexicon provenance independently of exercise transactions; e.g. catalog updates never rewrite past questions."""

    def verb_catalog(self) -> list[dict]:
        """Combine starter verbs with captured lemmas and click counts; e.g. vocabulary verbs receive selection priority."""

        from yt_whisper_subs import verb_syllabus

        with self._connect() as conn:
            conn.executemany("INSERT OR IGNORE INTO verb_lemmas(lemma,family,transitive) VALUES(?,?,?)", verb_syllabus.CATALOG)
            rows = conn.execute(
                "SELECT v.*,COUNT(c.id) AS clicks,COUNT(DISTINCT c.source_id) AS videos,"
                "COUNT(DISTINCT x.lookup_id) AS lookups FROM verb_lemmas v "
                "LEFT JOIN verb_vocab_links x ON x.verb_id=v.id AND EXISTS "
                "(SELECT 1 FROM vocab_lookups l WHERE l.id=x.lookup_id AND l.state='ready' "
                "AND l.ambiguous=0 AND l.updated_at=x.source_stamp) "
                "LEFT JOIN vocab_clicks c ON c.lookup_id=x.lookup_id GROUP BY v.id ORDER BY v.id")
            result = [dict(row) for row in rows]
            history = conn.execute(f"SELECT l.lemma_norm,COUNT(*) AS attempts,SUM(h.outcome='wrong') AS failures "
                                   f"FROM ({word_reviews.REVIEWS}) h JOIN vocab_lookups l ON l.id=h.lookup_id "
                                   "WHERE h.outcome IN ('correct','wrong') GROUP BY l.lemma_norm").fetchall()
            lexical = {row["lemma_norm"]: row for row in history}
        starters = {lemma for lemma, _, _ in verb_syllabus.CATALOG}
        return [{**row, "starter": row["lemma"] in starters,
                 "word_attempts": lexical[row["lemma"]]["attempts"] if row["lemma"] in lexical else 0,
                 "word_failures": lexical[row["lemma"]]["failures"] if row["lemma"] in lexical else 0} for row in result]

    def verb_vocab_pending(self, policy: dict) -> list[dict]:
        """Load newly translated or changed occurrences once per policy; e.g. repeated clicks reuse the same lexical decision."""

        with self._connect() as conn:
            rows = conn.execute(
                "SELECT l.id AS lookup_id,l.lemma,l.part_of_speech,l.context_json,l.meaning,l.lemma_meaning,l.expression,"
                "l.note,l.updated_at AS source_stamp FROM vocab_lookups l JOIN vocab_words w ON w.id=l.word_id "
                "LEFT JOIN verb_vocab_links x ON x.lookup_id=l.id WHERE w.language='nl' AND l.state='ready' "
                "AND l.ambiguous=0 AND TRIM(COALESCE(l.lemma,''))<>'' "
                f"AND EXISTS(SELECT 1 FROM ({vocabulary_db.OCCURRENCES}) e WHERE e.lookup_id=l.id) "
                "AND (x.lookup_id IS NULL OR x.source_stamp<>l.updated_at OR x.model<>:model "
                "OR x.reasoning<>:reasoning OR x.prompt_version<>:prompt_version "
                "OR (json_extract(x.evidence_json,'$.verdict.valid')=0 AND x.checked_at<:retry_before)) ORDER BY l.id",
                {**policy, "retry_before": time.time() - 86400})
            return [dict(row) for row in rows]

    def verb_vocab_save(self, rows: list[dict], results: list[dict], policy: dict) -> None:
        """Commit exact audited mappings and rejected occurrences together; e.g. invalid IDs cannot partially update the lexicon."""

        from yt_whisper_subs import verb_practice as verbs

        expected = {row["lookup_id"]: row for row in rows}
        checked = {}
        for item in results:
            lexeme = verbs.Lexeme.model_validate(item["lexeme"])
            verdict = verbs.LexemeCheck.model_validate(item["verdict"])
            if lexeme.lookup_id not in expected or lexeme.lookup_id in checked or verdict.lookup_id != lexeme.lookup_id:
                raise ValueError("Invalid vocabulary verb IDs")
            checked[lexeme.lookup_id] = (lexeme, verdict, item.get("strategy", "provider_audit"))
        if set(checked) != set(expected):
            raise ValueError("The provider omitted vocabulary verb decisions")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for lookup_id, (lexeme, verdict, strategy) in checked.items():
                current = conn.execute("SELECT state,updated_at FROM vocab_lookups WHERE id=?", (lookup_id,)).fetchone()
                if current is None or current["state"] != "ready" or current["updated_at"] != expected[lookup_id]["source_stamp"]:
                    continue
                verb_id = None
                if lexeme.is_verb and verdict.valid:
                    conn.execute("INSERT OR IGNORE INTO verb_lemmas(lemma,family,transitive) VALUES(?,?,?)",
                                 (lexeme.lemma, lexeme.family, int(lexeme.transitive)))
                    verb_id = conn.execute("SELECT id FROM verb_lemmas WHERE lemma=?", (lexeme.lemma,)).fetchone()[0]
                evidence = json.dumps({"lexeme": lexeme.model_dump(), "verdict": verdict.model_dump(),
                                       "strategy": strategy}, ensure_ascii=False)
                conn.execute("INSERT INTO verb_vocab_links VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(lookup_id) DO UPDATE "
                             "SET verb_id=excluded.verb_id,source_stamp=excluded.source_stamp,model=excluded.model,"
                             "reasoning=excluded.reasoning,prompt_version=excluded.prompt_version,"
                             "evidence_json=excluded.evidence_json,checked_at=excluded.checked_at",
                             (lookup_id, verb_id, expected[lookup_id]["source_stamp"], policy["model"], policy["reasoning"],
                              policy["prompt_version"], evidence, time.time()))
