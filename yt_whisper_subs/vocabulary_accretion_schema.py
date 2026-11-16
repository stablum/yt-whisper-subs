"""Relate derived vocabulary to real source events without inventing clicks or failures.

Example: reporting a quiz removes its confused-word and compound evidence from eligibility.
"""

from yt_whisper_subs import vocabulary_learning_db as learning
from yt_whisper_subs import vocabulary_cleanup as cleanup


SCHEMA = """
CREATE TABLE IF NOT EXISTS vocab_audits (
    id INTEGER PRIMARY KEY, cache_key TEXT NOT NULL UNIQUE, document_json TEXT NOT NULL,
    model TEXT NOT NULL, reasoning TEXT NOT NULL, version INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','working','verify','verifying','done','failed')),
    proposal_json TEXT, result_json TEXT, repair_json TEXT, error TEXT NOT NULL DEFAULT '',
    owner TEXT, lease_until REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS vocab_audit_sources (
    id INTEGER PRIMARY KEY, audit_id INTEGER NOT NULL REFERENCES vocab_audits(id),
    lookup_id INTEGER REFERENCES vocab_lookups(id),
    practice_id INTEGER REFERENCES practice_presentations(id),
    sentence_id INTEGER REFERENCES sentence_presentations(id),
    applied INTEGER NOT NULL DEFAULT 0 CHECK(applied IN (0,1)),
    CHECK((lookup_id IS NOT NULL)+(practice_id IS NOT NULL)+(sentence_id IS NOT NULL)=1),
    UNIQUE(audit_id,lookup_id), UNIQUE(audit_id,practice_id), UNIQUE(audit_id,sentence_id)
);
CREATE TABLE IF NOT EXISTS vocab_additions (
    id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES vocab_audit_sources(id),
    lookup_id INTEGER NOT NULL REFERENCES vocab_lookups(id),
    role TEXT NOT NULL CHECK(role IN ('intended','confused','component')),
    parent TEXT NOT NULL DEFAULT '', review INTEGER NOT NULL CHECK(review IN (0,1)),
    CHECK(role<>'component' OR review=0), UNIQUE(source_id,lookup_id,role,parent)
);
CREATE INDEX IF NOT EXISTS idx_vocab_audit_work ON vocab_audits(state,lease_until,id);
CREATE INDEX IF NOT EXISTS idx_vocab_addition_lookup ON vocab_additions(lookup_id);
"""

# These joins retain native source foreign keys, scheduling policy and source validity.
SOURCE = """
FROM vocab_audit_sources a JOIN vocab_audits j ON j.id=a.audit_id
LEFT JOIN practice_presentations p ON p.id=a.practice_id
LEFT JOIN practice_sessions ps ON ps.id=p.session_id
LEFT JOIN sentence_presentations s ON s.id=a.sentence_id
LEFT JOIN sentence_sessions ss ON ss.id=s.session_id
LEFT JOIN vocab_lookups root ON root.id=a.lookup_id
"""
MISTAKE_VALID = "COALESCE(p.outcome,s.outcome)='wrong'"
LIVE_LOOKUPS = f"""
SELECT e.lookup_id FROM (
    SELECT lookup_id FROM ({learning.OCCURRENCES})
    UNION
    SELECT lookup_id FROM vocab_retained_components
    UNION
    SELECT d.lookup_id {SOURCE} JOIN vocab_additions d ON d.source_id=a.id
    WHERE d.role<>'component' AND {MISTAKE_VALID}
) e JOIN vocab_lookups l ON l.id=e.lookup_id JOIN vocab_words w ON w.id=l.word_id
WHERE {cleanup.ELIGIBLE}
"""
VALID = f"({MISTAKE_VALID} OR a.lookup_id IN ({LIVE_LOOKUPS}))"
ACTIVE_JOB = f"EXISTS(SELECT 1 {SOURCE} WHERE a.audit_id=job.id AND {VALID})"

OCCURRENCES = f"""
SELECT 'addition:'||d.id AS event_key,
       CASE WHEN d.role='component' THEN 'component' WHEN a.practice_id IS NOT NULL THEN 'quiz' ELSE 'listening' END AS origin,
       d.lookup_id,l.lemma AS surface,COALESCE(p.answered_at,s.answered_at,root.updated_at) AS occurred_at,
       NULL AS source_id,NULL AS source_key,NULL AS video_id,
       CASE WHEN d.role='component' THEN 'Component of '||d.parent
            WHEN a.practice_id IS NOT NULL THEN 'Word quiz' ELSE 'Listening practice' END AS title,
       '' AS url,'' AS path,NULL AS position_seconds,NULL AS cue_start_ms,NULL AS cue_end_ms,
       NULL AS token_start,NULL AS token_end,
       COALESCE(json_extract(j.document_json,'$.answer'),'') AS typed,
       COALESCE(json_extract(j.document_json,'$.dutch'),d.parent) AS correction,d.role
{SOURCE} JOIN vocab_additions d ON d.source_id=a.id JOIN vocab_lookups l ON l.id=d.lookup_id
WHERE {VALID}
"""

# Collapse multiple error annotations of one identity into one failed observation per answer.
REVIEWS = f"""
SELECT 'accretion' AS origin,MIN(d.id) AS event_id,l.word_id,d.lookup_id,
       CASE WHEN ss.direction='listen_nl' THEN 'nl_en' ELSE ps.direction END AS direction,
       COALESCE(p.answered_at,s.answered_at) AS answered_at,COALESCE(p.elapsed_ms,s.elapsed_ms) AS elapsed_ms,
       0 AS correct,0 AS unsure,COALESCE(ps.scheduler_json,ss.scheduler_json) AS scheduler_json,
       COALESCE(ps.include_early,json_extract(ss.options_json,'$.include_early')) AS include_early,
       CASE WHEN {MISTAKE_VALID} THEN 'wrong' ELSE 'invalid' END AS outcome,l.lemma AS word
{SOURCE} JOIN vocab_additions d ON d.source_id=a.id JOIN vocab_lookups l ON l.id=d.lookup_id
WHERE d.review=1 GROUP BY a.id,l.word_id
"""
