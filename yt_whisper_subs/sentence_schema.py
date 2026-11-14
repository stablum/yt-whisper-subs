"""Relational sentence, vocabulary and grammatical-form review evidence.

Example: each typed attempt has one whole-sentence review and independently scheduled saved-word assessments.
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS sentence_questions (
    id INTEGER PRIMARY KEY,
    direction TEXT NOT NULL CHECK(direction IN ('nl_en','en_nl')),
    anchor_word_id INTEGER NOT NULL REFERENCES vocab_words(id),
    focus_key TEXT NOT NULL,
    scene TEXT NOT NULL,
    body_json TEXT NOT NULL,
    validation_json TEXT NOT NULL,
    model TEXT NOT NULL,
    reasoning TEXT NOT NULL,
    prompt_version INTEGER NOT NULL,
    created_at REAL NOT NULL,
    flagged_at REAL
);
CREATE INDEX IF NOT EXISTS idx_sentence_cache ON sentence_questions(direction,focus_key,model,reasoning,prompt_version,flagged_at);
CREATE TABLE IF NOT EXISTS sentence_question_words (
    question_id INTEGER NOT NULL REFERENCES sentence_questions(id),
    lookup_id INTEGER NOT NULL REFERENCES vocab_lookups(id),
    ordinal INTEGER NOT NULL,
    PRIMARY KEY(question_id,lookup_id),
    UNIQUE(question_id,ordinal)
);
CREATE TABLE IF NOT EXISTS sentence_sessions (
    id TEXT PRIMARY KEY,
    direction TEXT NOT NULL CHECK(direction IN ('nl_en','en_nl')),
    options_json TEXT NOT NULL,
    scheduler_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    finished_at REAL,
    status TEXT NOT NULL CHECK(status IN ('active','completed','stopped'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sentence_active ON sentence_sessions(status) WHERE status='active';
CREATE TABLE IF NOT EXISTS sentence_presentations (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sentence_sessions(id),
    question_id INTEGER NOT NULL REFERENCES sentence_questions(id),
    ordinal INTEGER NOT NULL CHECK(ordinal>=1),
    shown_at REAL,
    answered_at REAL,
    answer_text TEXT CHECK(answer_text IS NULL OR LENGTH(TRIM(answer_text)) BETWEEN 1 AND 2000),
    elapsed_ms INTEGER CHECK(elapsed_ms>=0),
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
    CHECK((answer_text IS NULL)=(answered_at IS NULL)),
    CHECK(answer_text IS NULL OR (shown_at IS NOT NULL AND elapsed_ms IS NOT NULL)),
    CHECK(outcome NOT IN ('correct','wrong') OR (answer_text IS NOT NULL AND grade_json IS NOT NULL AND graded_at IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_sentence_answers ON sentence_presentations(answered_at)
    WHERE answered_at IS NOT NULL AND answer_text IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_sentence_presentations_question ON sentence_presentations(question_id);
CREATE TABLE IF NOT EXISTS sentence_assessments (
    presentation_id INTEGER NOT NULL REFERENCES sentence_presentations(id),
    lookup_id INTEGER NOT NULL REFERENCES vocab_lookups(id),
    meaning_correct INTEGER NOT NULL CHECK(meaning_correct IN (0,1)),
    target_used INTEGER NOT NULL CHECK(target_used IN (0,1)),
    form_correct INTEGER CHECK(form_correct IN (0,1)),
    explanation TEXT NOT NULL,
    scheduled INTEGER NOT NULL CHECK(scheduled IN (0,1)),
    rating INTEGER CHECK(rating BETWEEN 1 AND 4),
    recall REAL CHECK(recall BETWEEN 0 AND 1),
    due_at REAL,
    before_json TEXT,
    after_json TEXT,
    log_json TEXT,
    PRIMARY KEY(presentation_id,lookup_id)
);
CREATE TABLE IF NOT EXISTS sentence_forms (
    presentation_id INTEGER NOT NULL,
    lookup_id INTEGER NOT NULL,
    feature TEXT NOT NULL,
    correct INTEGER CHECK(correct IN (0,1)),
    explanation TEXT NOT NULL,
    PRIMARY KEY(presentation_id,lookup_id,feature),
    FOREIGN KEY(presentation_id,lookup_id) REFERENCES sentence_assessments(presentation_id,lookup_id)
);
CREATE INDEX IF NOT EXISTS idx_sentence_assessment_word ON sentence_assessments(lookup_id,presentation_id);
CREATE TABLE IF NOT EXISTS sentence_errors (
    id INTEGER PRIMARY KEY,
    presentation_id INTEGER NOT NULL REFERENCES sentence_presentations(id),
    ordinal INTEGER NOT NULL,
    kind TEXT NOT NULL,
    evidence TEXT NOT NULL,
    correction TEXT NOT NULL,
    explanation TEXT NOT NULL,
    UNIQUE(presentation_id,ordinal)
);
CREATE TABLE IF NOT EXISTS sentence_error_words (
    error_id INTEGER NOT NULL REFERENCES sentence_errors(id),
    lookup_id INTEGER NOT NULL REFERENCES vocab_lookups(id),
    PRIMARY KEY(error_id,lookup_id)
);
CREATE TABLE IF NOT EXISTS sentence_states (
    word_id INTEGER NOT NULL REFERENCES vocab_words(id),
    direction TEXT NOT NULL CHECK(direction IN ('nl_en','en_nl')),
    card_json TEXT NOT NULL,
    due_at REAL NOT NULL,
    PRIMARY KEY(word_id,direction)
);
CREATE TABLE IF NOT EXISTS sentence_question_states (
    question_id INTEGER PRIMARY KEY REFERENCES sentence_questions(id),
    card_json TEXT NOT NULL,
    due_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sentence_due ON sentence_question_states(due_at);
"""
