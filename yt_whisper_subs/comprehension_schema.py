"""Relate passages, immutable answers, lexical observations and narration.

Example: a false friend in an open answer retains its source without manufacturing a subtitle click.
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS comprehension_passages (
    id INTEGER PRIMARY KEY, content_key TEXT NOT NULL, body_json TEXT NOT NULL,
    targets_json TEXT NOT NULL, model TEXT NOT NULL, reasoning TEXT NOT NULL, version INTEGER NOT NULL,
    validation_json TEXT, error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
    flagged_at REAL, flag_reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_comprehension_pool ON comprehension_passages(content_key,model,version,id);
CREATE TABLE IF NOT EXISTS comprehension_questions (
    id INTEGER PRIMARY KEY, passage_id INTEGER NOT NULL REFERENCES comprehension_passages(id),
    ordinal INTEGER NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('mc','open')), body_json TEXT NOT NULL,
    UNIQUE(passage_id,ordinal)
);
CREATE TABLE IF NOT EXISTS comprehension_targets (
    passage_id INTEGER NOT NULL REFERENCES comprehension_passages(id),
    word_id INTEGER NOT NULL REFERENCES vocab_words(id), lookup_id INTEGER NOT NULL REFERENCES vocab_lookups(id),
    PRIMARY KEY(passage_id,word_id)
);
CREATE TABLE IF NOT EXISTS comprehension_sessions (
    id TEXT PRIMARY KEY, passage_id INTEGER NOT NULL REFERENCES comprehension_passages(id),
    direction TEXT NOT NULL CHECK(direction IN ('read','listen')), options_json TEXT NOT NULL,
    scheduler_json TEXT NOT NULL, created_at REAL NOT NULL, shown_at REAL, finished_at REAL,
    status TEXT NOT NULL CHECK(status IN ('ready','active','completed','stopped'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_comprehension_active ON comprehension_sessions(direction) WHERE status='active';
CREATE INDEX IF NOT EXISTS idx_comprehension_ready ON comprehension_sessions(direction,status);
CREATE TABLE IF NOT EXISTS comprehension_presentations (
    id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES comprehension_sessions(id),
    question_id INTEGER NOT NULL REFERENCES comprehension_questions(id), ordinal INTEGER NOT NULL,
    shown_at REAL, answer_text TEXT, selected_idx INTEGER CHECK(selected_idx BETWEEN 0 AND 3),
    answered_at REAL, elapsed_ms INTEGER NOT NULL DEFAULT 0,
    outcome TEXT CHECK(outcome IN ('correct','wrong','invalid')), grade_json TEXT,
    score REAL CHECK(score BETWEEN 0 AND 1), language_correct INTEGER CHECK(language_correct IN (0,1)),
    graded_at REAL, viewed_at REAL, model TEXT, reasoning TEXT, version INTEGER,
    UNIQUE(session_id,ordinal), UNIQUE(session_id,question_id)
);
CREATE INDEX IF NOT EXISTS idx_comprehension_answers ON comprehension_presentations(answered_at);
CREATE TABLE IF NOT EXISTS comprehension_drafts (
    presentation_id INTEGER PRIMARY KEY REFERENCES comprehension_presentations(id),
    text TEXT NOT NULL, elapsed_ms INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS comprehension_checks (
    presentation_id INTEGER PRIMARY KEY REFERENCES comprehension_presentations(id),
    state TEXT NOT NULL CHECK(state IN ('queued','checking','done','failed')),
    owner TEXT, lease_until REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '', proposal_json TEXT
);
CREATE TABLE IF NOT EXISTS comprehension_assessments (
    id INTEGER PRIMARY KEY, presentation_id INTEGER NOT NULL REFERENCES comprehension_presentations(id),
    word_id INTEGER NOT NULL REFERENCES vocab_words(id), lookup_id INTEGER NOT NULL REFERENCES vocab_lookups(id),
    direction TEXT NOT NULL CHECK(direction IN ('nl_en','en_nl')),
    role TEXT NOT NULL CHECK(role IN ('target','intended','confused')),
    correct INTEGER NOT NULL CHECK(correct IN (0,1)),
    UNIQUE(presentation_id,word_id,direction)
);
CREATE INDEX IF NOT EXISTS idx_comprehension_word ON comprehension_assessments(word_id,direction);
CREATE TABLE IF NOT EXISTS comprehension_errors (
    id INTEGER PRIMARY KEY, presentation_id INTEGER NOT NULL REFERENCES comprehension_presentations(id),
    ordinal INTEGER NOT NULL, body_json TEXT NOT NULL, UNIQUE(presentation_id,ordinal)
);
CREATE TABLE IF NOT EXISTS comprehension_error_words (
    error_id INTEGER NOT NULL REFERENCES comprehension_errors(id),
    role TEXT NOT NULL CHECK(role IN ('intended','confused')),
    lookup_id INTEGER NOT NULL REFERENCES vocab_lookups(id), PRIMARY KEY(error_id,role)
);
CREATE TABLE IF NOT EXISTS comprehension_audio (
    passage_id INTEGER NOT NULL REFERENCES comprehension_passages(id), segment INTEGER NOT NULL,
    cache_key TEXT NOT NULL REFERENCES listening_assets(cache_key), PRIMARY KEY(passage_id,segment,cache_key)
);
CREATE TABLE IF NOT EXISTS comprehension_plays (
    id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES comprehension_sessions(id),
    cache_key TEXT NOT NULL REFERENCES listening_assets(cache_key), segment INTEGER NOT NULL,
    speed TEXT NOT NULL CHECK(speed IN ('normal','slow')), played_at REAL NOT NULL
);
"""


REVIEWS = """
SELECT 'comprehension_'||s.direction AS origin,a.id AS event_id,a.word_id,a.lookup_id,a.direction,
       p.answered_at,p.elapsed_ms,a.correct,0 AS unsure,s.scheduler_json,
       json_extract(s.options_json,'$.include_early') AS include_early,
       CASE WHEN p.outcome='invalid' OR t.flagged_at IS NOT NULL THEN 'invalid'
            WHEN a.correct=1 THEN 'correct' ELSE 'wrong' END AS outcome,l.lemma AS word
FROM comprehension_assessments a JOIN comprehension_presentations p ON p.id=a.presentation_id
JOIN comprehension_sessions s ON s.id=p.session_id JOIN comprehension_passages t ON t.id=s.passage_id
JOIN vocab_lookups l ON l.id=a.lookup_id
"""


OCCURRENCES = """
SELECT 'comprehension:'||e.id||':'||w.role AS event_key,'comprehension' AS origin,w.lookup_id,l.lemma AS surface,
       p.answered_at AS occurred_at,NULL AS source_id,NULL AS source_key,NULL AS video_id,
       CASE WHEN s.direction='read' THEN 'Leesbegrip' ELSE 'Luisterbegrip' END AS title,
       '' AS url,'' AS path,NULL AS position_seconds,NULL AS cue_start_ms,NULL AS cue_end_ms,
       NULL AS token_start,NULL AS token_end,json_extract(e.body_json,'$.typed') AS typed,
       json_extract(p.grade_json,'$.corrected') AS correction,w.role
FROM comprehension_errors e JOIN comprehension_error_words w ON w.error_id=e.id
JOIN vocab_lookups l ON l.id=w.lookup_id JOIN comprehension_presentations p ON p.id=e.presentation_id
JOIN comprehension_sessions s ON s.id=p.session_id JOIN comprehension_passages t ON t.id=s.passage_id
WHERE p.outcome IN ('correct','wrong') AND t.flagged_at IS NULL
"""


PRESENTATION = """
SELECT p.*,q.body_json,q.kind,s.direction,s.passage_id,s.options_json,s.scheduler_json,s.status,
       t.flagged_at,c.state AS check_state,c.error AS check_error
FROM comprehension_presentations p JOIN comprehension_questions q ON q.id=p.question_id
JOIN comprehension_sessions s ON s.id=p.session_id JOIN comprehension_passages t ON t.id=s.passage_id
LEFT JOIN comprehension_checks c ON c.presentation_id=p.id
"""
