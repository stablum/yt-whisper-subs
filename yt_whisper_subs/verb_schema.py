"""Relational verb exercise, submission and diagnostic history.

Example: an answer submitted before a network failure stays durable and can be checked again later.
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS verb_lemmas (
    id INTEGER PRIMARY KEY,
    lemma TEXT NOT NULL UNIQUE,
    family TEXT NOT NULL,
    transitive INTEGER NOT NULL CHECK(transitive IN (0,1))
);
CREATE TABLE IF NOT EXISTS verb_targets (
    id INTEGER PRIMARY KEY,
    verb_id INTEGER NOT NULL REFERENCES verb_lemmas(id),
    tense TEXT NOT NULL,
    subject TEXT NOT NULL,
    structure TEXT NOT NULL,
    UNIQUE(verb_id,tense,subject,structure)
);
CREATE TABLE IF NOT EXISTS verb_questions (
    id INTEGER PRIMARY KEY,
    target_id INTEGER NOT NULL REFERENCES verb_targets(id),
    body_json TEXT NOT NULL,
    validation_json TEXT NOT NULL,
    model TEXT NOT NULL,
    reasoning TEXT NOT NULL,
    prompt_version INTEGER NOT NULL,
    created_at REAL NOT NULL,
    flagged_at REAL
);
CREATE INDEX IF NOT EXISTS idx_verb_questions ON verb_questions(target_id,model,reasoning,prompt_version,flagged_at);
CREATE TABLE IF NOT EXISTS verb_sessions (
    id TEXT PRIMARY KEY,
    options_json TEXT NOT NULL,
    scheduler_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    finished_at REAL,
    status TEXT NOT NULL CHECK(status IN ('active','completed','stopped'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_verb_active ON verb_sessions(status) WHERE status='active';
CREATE TABLE IF NOT EXISTS verb_presentations (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES verb_sessions(id),
    question_id INTEGER NOT NULL REFERENCES verb_questions(id),
    ordinal INTEGER NOT NULL CHECK(ordinal >= 1),
    shown_at REAL,
    answered_at REAL,
    answer_text TEXT CHECK(answer_text IS NULL OR LENGTH(TRIM(answer_text)) BETWEEN 1 AND 2000),
    elapsed_ms INTEGER CHECK(elapsed_ms >= 0),
    outcome TEXT CHECK(outcome IN ('correct','wrong','invalid')),
    grade_json TEXT,
    grader_model TEXT,
    grader_reasoning TEXT,
    grade_version INTEGER,
    graded_at REAL,
    rating INTEGER CHECK(rating BETWEEN 1 AND 4),
    recall REAL CHECK(recall BETWEEN 0 AND 1),
    due_at REAL,
    before_json TEXT,
    after_json TEXT,
    log_json TEXT,
    UNIQUE(session_id,ordinal),
    CHECK((answer_text IS NULL) = (answered_at IS NULL)),
    CHECK(answer_text IS NULL OR (shown_at IS NOT NULL AND elapsed_ms IS NOT NULL)),
    CHECK(outcome NOT IN ('correct','wrong') OR (answer_text IS NOT NULL AND grade_json IS NOT NULL AND graded_at IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_verb_presentations_question ON verb_presentations(question_id);
CREATE INDEX IF NOT EXISTS idx_verb_answers ON verb_presentations(answered_at)
    WHERE answered_at IS NOT NULL AND answer_text IS NOT NULL;
CREATE TABLE IF NOT EXISTS verb_assessments (
    presentation_id INTEGER NOT NULL REFERENCES verb_presentations(id),
    axis TEXT NOT NULL CHECK(axis IN ('tense','subject','structure','verb')),
    correct INTEGER NOT NULL CHECK(correct IN (0,1)),
    PRIMARY KEY(presentation_id,axis)
);
CREATE TABLE IF NOT EXISTS verb_errors (
    id INTEGER PRIMARY KEY,
    presentation_id INTEGER NOT NULL REFERENCES verb_presentations(id),
    ordinal INTEGER NOT NULL,
    kind TEXT NOT NULL,
    axis TEXT NOT NULL,
    evidence TEXT NOT NULL,
    correction TEXT NOT NULL,
    explanation TEXT NOT NULL,
    FOREIGN KEY(presentation_id,axis) REFERENCES verb_assessments(presentation_id,axis),
    UNIQUE(presentation_id,ordinal)
);
CREATE INDEX IF NOT EXISTS idx_verb_error_kind ON verb_errors(kind,presentation_id);
CREATE TABLE IF NOT EXISTS verb_states (
    target_id INTEGER PRIMARY KEY REFERENCES verb_targets(id),
    card_json TEXT NOT NULL,
    due_at REAL NOT NULL
);
"""
