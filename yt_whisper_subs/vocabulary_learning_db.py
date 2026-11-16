"""Relational practice mistakes linked to genuine dictionary entries and review events.

Example: practice-origin words have zero video clicks and retain the exact original correction.
"""

from __future__ import annotations

import json
import time

from yt_whisper_subs import vocabulary
from yt_whisper_subs import comprehension_schema


SCHEMA = """
CREATE TABLE IF NOT EXISTS vocab_enrichments (
    id INTEGER PRIMARY KEY,
    sentence_id INTEGER UNIQUE REFERENCES sentence_presentations(id),
    verb_id INTEGER UNIQUE REFERENCES verb_presentations(id),
    model TEXT NOT NULL,reasoning TEXT NOT NULL,version INTEGER NOT NULL,
    response_json TEXT NOT NULL,created_at REAL NOT NULL,
    CHECK((sentence_id IS NOT NULL)+(verb_id IS NOT NULL)=1)
);
CREATE TABLE IF NOT EXISTS vocab_errors (
    id INTEGER PRIMARY KEY,
    enrichment_id INTEGER NOT NULL REFERENCES vocab_enrichments(id),
    ordinal INTEGER NOT NULL,typed TEXT NOT NULL,dutch TEXT NOT NULL,english TEXT NOT NULL,note TEXT NOT NULL,
    UNIQUE(enrichment_id,ordinal)
);
CREATE TABLE IF NOT EXISTS vocab_error_words (
    error_id INTEGER NOT NULL REFERENCES vocab_errors(id),
    role TEXT NOT NULL CHECK(role IN ('intended','confused')),
    lookup_id INTEGER NOT NULL REFERENCES vocab_lookups(id),
    PRIMARY KEY(error_id,role)
);
CREATE INDEX IF NOT EXISTS idx_vocab_error_lookup ON vocab_error_words(lookup_id);
"""

# Both sources expose real provenance; practice events never pretend to be video clicks.
OCCURRENCES = f"""
SELECT 'click:'||c.id AS event_key,'click' AS origin,c.lookup_id,c.surface,c.clicked_at AS occurred_at,
       c.source_id,s.source_key,s.video_id,s.title,s.url,s.path,c.position_seconds,c.cue_start_ms,c.cue_end_ms,
       c.token_start,c.token_end,'' AS typed,'' AS correction,'' AS role
FROM vocab_clicks c JOIN vocab_sources s ON s.id=c.source_id
UNION ALL
SELECT 'error:'||e.id||':'||w.role,CASE WHEN x.sentence_id IS NULL THEN 'verb' ELSE 'sentence' END,
       w.lookup_id,l.lemma,COALESCE(p.answered_at,v.answered_at),NULL,NULL,NULL,
       CASE WHEN x.sentence_id IS NULL THEN 'Verb practice' ELSE 'Sentence practice' END,'','',NULL,NULL,NULL,NULL,NULL,
       e.typed,e.dutch,w.role
FROM vocab_errors e JOIN vocab_enrichments x ON x.id=e.enrichment_id
JOIN vocab_error_words w ON w.error_id=e.id JOIN vocab_lookups l ON l.id=w.lookup_id
LEFT JOIN sentence_presentations p ON p.id=x.sentence_id LEFT JOIN verb_presentations v ON v.id=x.verb_id
WHERE COALESCE(p.outcome,v.outcome) IN ('correct','wrong')
UNION ALL
{comprehension_schema.OCCURRENCES}
"""


class LearningDbMixin:
    """Commit corrected concepts with their source and shared failures; e.g. retries cannot add duplicate vocabulary mistakes."""

    def learning_captured(self, kind: str, presentation_id: int) -> bool:
        """Check enrichment without requesting the model; e.g. historical recovery is cached even when no new words were found."""

        if kind not in {"sentence", "verb"}:
            raise ValueError("Unknown typed practice mode")
        with self._connect() as conn:
            return conn.execute(f"SELECT 1 FROM vocab_enrichments WHERE {kind}_id=?", (presentation_id,)).fetchone() is not None

    def _learning_save(self, conn, kind: str, row: dict, corrections, provenance: dict) -> None:
        """Keep lexical facts and their failed review updates atomic; e.g. both veilig and zeker receive one source-linked Again."""

        from yt_whisper_subs import vocabulary_learning

        document = vocabulary_learning.document(kind, row)
        capture = vocabulary_learning.Capture(row=row, context=document, provenance=provenance)
        corrections.check(document)
        cursor = conn.execute(f"INSERT OR IGNORE INTO vocab_enrichments({kind}_id,model,reasoning,version,response_json,created_at) "
                              "VALUES(?,?,?,?,?,?)", (row["id"], provenance["model"], provenance["reasoning"], vocabulary_learning.VERSION,
                                                      corrections.model_dump_json(), time.time()))
        if not cursor.rowcount:
            return
        enrichment_id = cursor.lastrowid
        for ordinal, fix in enumerate(corrections.vocabulary):
            error = conn.execute("INSERT INTO vocab_errors(enrichment_id,ordinal,typed,dutch,english,note) VALUES(?,?,?,?,?,?)",
                                 (enrichment_id, ordinal, fix.typed, " … ".join(fix.dutch), " … ".join(fix.english), fix.note)).lastrowid
            for role in ("intended", "confused"):
                lexeme = getattr(fix, role)
                if lexeme is None or not self._word_allowed(conn, "nl", lexeme.lemma):
                    continue
                lookup_id = self._learning_lookup(conn, fix, role, capture)
                conn.execute("INSERT INTO vocab_error_words VALUES(?,?,?)", (error, role, lookup_id))
                self._word_observe(conn, kind, row, lookup_id, False)
                self._accretion_compound(conn, lookup_id)

    @staticmethod
    def _learning_lookup(conn, fix, role: str, capture) -> int:
        """Reuse an existing dictionary identity while retaining the new context; e.g. a captured veilige and corrected veilig share one word."""

        row, document, provenance = capture.row, capture.context, capture.provenance
        lexeme = getattr(fix, role)
        norm = vocabulary.normalized(lexeme.lemma)
        existing = conn.execute("SELECT w.id FROM vocab_words w LEFT JOIN vocab_lookups l ON l.word_id=w.id "
                                "WHERE w.language='nl' AND (w.normalized=? OR l.lemma_norm=?) "
                                "ORDER BY w.normalized=? DESC,w.id LIMIT 1", (norm, norm, norm)).fetchone()
        if existing:
            word_id = existing[0]
        else:
            word_id = conn.execute("INSERT INTO vocab_words(language,normalized) VALUES('nl',?)", (norm,)).lastrowid
        sentence = document["dutch"]
        form = fix.dutch[0]
        if role == "confused" and document["direction"] in {"en_nl", "listen_nl"}:
            sentence, form = document["answer"], fix.typed
        elif role == "confused":
            sentence = form = lexeme.lemma
        start = sentence.casefold().find(form.casefold())
        context = {"language": "nl", "target_language": "en", "word": lexeme.lemma, "sentence": sentence,
                   "span": [start, start + len(form)] if start >= 0 else None, "before": "", "after": "",
                   "english": document["english"] if role == "intended" else lexeme.meaning}
        key = f"learning:{document['origin']}:{word_id}:{row['id']}:{provenance['model']}:{role}:{lexeme.meaning}"
        note = fix.note
        if document["origin"] != "component":
            note += f" Learner confusion: {fix.typed} → {' … '.join(fix.dutch)}."
        conn.execute("INSERT OR IGNORE INTO vocab_lookups(cache_key,word_id,context_json,model,reasoning,prompt_version,state,"
                              "meaning,lemma,lemma_norm,lemma_meaning,part_of_speech,note,expression,ambiguous,updated_at) "
                              "VALUES(?,?,?,?,?,1,'ready',?,?,?,?,?,?,?,0,?)",
                              (key, word_id, json.dumps(context, ensure_ascii=False), provenance["model"], provenance["reasoning"],
                               lexeme.meaning, lexeme.lemma, norm, lexeme.meaning, lexeme.part_of_speech,
                               note, "", row["answered_at"]))
        return conn.execute("SELECT id FROM vocab_lookups WHERE cache_key=?", (key,)).fetchone()[0]

    def learning_recover(self, kind: str, presentation_id: int, provider=None) -> None:
        """Enrich an existing graded attempt once; e.g. the user's earlier safer/certain example gets the new shared treatment."""

        from yt_whisper_subs import vocabulary_learning

        if self.learning_captured(kind, presentation_id):
            return
        row = getattr(self, f"{kind}_get")(presentation_id)
        if row is None or row["outcome"] not in {"correct", "wrong"}:
            raise ValueError("Only a valid graded attempt can add vocabulary")
        provider = provider or vocabulary_learning.LearningGenerator()
        corrections = provider.extract(vocabulary_learning.document(kind, row))
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = getattr(self, f"{kind}_get")(presentation_id)
            if current["outcome"] not in {"correct", "wrong"}:
                raise ValueError("The exercise was reported during vocabulary recovery")
            self._learning_save(conn, kind, current, corrections, {"model": provider.model, "reasoning": provider.reasoning})
