CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE SCHEMA IF NOT EXISTS rag;
CREATE SCHEMA IF NOT EXISTS scheduling;

-- Catalog is entirely synthetic. tuss_code is a legacy name for MOCK identifiers.
CREATE TABLE IF NOT EXISTS rag.exams_catalog (
    id SERIAL PRIMARY KEY,
    tuss_code VARCHAR(16) NOT NULL UNIQUE,
    official_name VARCHAR(255) NOT NULL,
    synonyms TEXT NOT NULL,
    category VARCHAR(100) NOT NULL,
    fasting_hours INT NOT NULL DEFAULT 0,
    preparation_instructions TEXT NOT NULL,
    turnaround_days INT NOT NULL DEFAULT 1,
    embedding vector(384) NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_exams_embedding_hnsw
    ON rag.exams_catalog USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
CREATE INDEX IF NOT EXISTS idx_exams_name_trgm
    ON rag.exams_catalog USING gin (official_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_exams_synonyms_trgm
    ON rag.exams_catalog USING gin (synonyms gin_trgm_ops);

CREATE TABLE IF NOT EXISTS scheduling.appointments (
    protocol_id VARCHAR(32) PRIMARY KEY,
    patient_reference_hash VARCHAR(64) NOT NULL DEFAULT 'ANONYMIZED_PATIENT',
    preferred_unit VARCHAR(120) NOT NULL DEFAULT 'Unidade Central - Matriz',
    status VARCHAR(32) NOT NULL DEFAULT 'CONFIRMED' CHECK (status IN ('CONFIRMED', 'PARTIAL')),
    unmatched_terms TEXT[] NOT NULL DEFAULT '{}',
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS scheduling.appointment_items (
    id SERIAL PRIMARY KEY,
    protocol_id VARCHAR(32) NOT NULL REFERENCES scheduling.appointments(protocol_id) ON DELETE CASCADE,
    tuss_code VARCHAR(16) NOT NULL,
    official_name VARCHAR(255) NOT NULL,
    original_query TEXT NOT NULL,
    category VARCHAR(100) NOT NULL,
    confidence_score DOUBLE PRECISION NOT NULL CHECK (confidence_score BETWEEN 0 AND 1),
    fasting_hours INT NOT NULL DEFAULT 0
);
