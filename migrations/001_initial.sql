CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS documents (
 id text PRIMARY KEY, candidate text NOT NULL, path text NOT NULL, group_key text NOT NULL,
 kind text NOT NULL, current_version text, deleted boolean NOT NULL DEFAULT false,
 metadata jsonb NOT NULL DEFAULT '{}', UNIQUE(candidate,path)
);
CREATE TABLE IF NOT EXISTS versions (
 id text PRIMARY KEY, document_id text NOT NULL REFERENCES documents(id), hash text NOT NULL,
 extracted_at timestamptz NOT NULL DEFAULT now(), status text NOT NULL, warnings jsonb NOT NULL DEFAULT '[]',
 text_length integer NOT NULL DEFAULT 0, UNIQUE(document_id,hash)
);
CREATE TABLE IF NOT EXISTS chunks (
 id bigserial PRIMARY KEY, version_id text NOT NULL REFERENCES versions(id), ordinal integer NOT NULL,
 section text NOT NULL, locator jsonb NOT NULL, body text NOT NULL, embedding vector,
 embedding_model text NOT NULL,
 lex tsvector GENERATED ALWAYS AS (to_tsvector('english', body)) STORED,
 UNIQUE(version_id,ordinal)
);
CREATE INDEX IF NOT EXISTS chunks_lex ON chunks USING gin(lex);
CREATE INDEX IF NOT EXISTS chunks_version ON chunks(version_id);
CREATE TABLE IF NOT EXISTS facts (
 id text PRIMARY KEY, candidate text NOT NULL, kind text NOT NULL, key text NOT NULL,
 value jsonb NOT NULL, status text NOT NULL, scope text NOT NULL DEFAULT 'global',
 provenance jsonb NOT NULL, effective_from text, effective_to text,
 verified_at timestamptz NOT NULL DEFAULT now(), supersedes text REFERENCES facts(id),
 active boolean NOT NULL DEFAULT true, evidence_versions jsonb NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS facts_lookup ON facts(candidate,key,scope) WHERE active;
CREATE TABLE IF NOT EXISTS answer_cache (
 key text PRIMARY KEY, candidate text NOT NULL, answer jsonb NOT NULL,
 fact_ids jsonb NOT NULL, version_ids jsonb NOT NULL, policy text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), valid boolean NOT NULL DEFAULT true
);
CREATE TABLE IF NOT EXISTS applications (
 id text PRIMARY KEY, candidate text NOT NULL, employer text NOT NULL, role text NOT NULL,
 requisition text NOT NULL, state text NOT NULL, payload jsonb NOT NULL DEFAULT '{}',
 score jsonb NOT NULL DEFAULT '{}', official jsonb NOT NULL DEFAULT '{}',
 revision integer NOT NULL DEFAULT 0, paused boolean NOT NULL DEFAULT false,
 resume_state text, imported boolean NOT NULL DEFAULT false,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(candidate,employer,requisition)
);
CREATE TABLE IF NOT EXISTS approvals (
 id text PRIMARY KEY, application_id text NOT NULL REFERENCES applications(id), payload_hash text NOT NULL,
 actor text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), invalidated boolean NOT NULL DEFAULT false
);
CREATE TABLE IF NOT EXISTS attempts (
 id text PRIMARY KEY, application_id text NOT NULL REFERENCES applications(id), payload_hash text NOT NULL,
 approval_id text NOT NULL REFERENCES approvals(id), state text NOT NULL,
 started_at timestamptz NOT NULL DEFAULT now(), result jsonb NOT NULL DEFAULT '{}',
 UNIQUE(application_id,payload_hash)
);
CREATE TABLE IF NOT EXISTS receipts (
 id text PRIMARY KEY, application_id text NOT NULL REFERENCES applications(id), attempt_id text REFERENCES attempts(id),
 kind text NOT NULL, evidence jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS artifacts (
 id text PRIMARY KEY, application_id text REFERENCES applications(id), path text NOT NULL UNIQUE,
 hash text NOT NULL, kind text NOT NULL, attached boolean NOT NULL DEFAULT false,
 immutable boolean NOT NULL DEFAULT false, claims jsonb NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS questions (
 id text PRIMARY KEY, application_id text NOT NULL REFERENCES applications(id), key text NOT NULL,
 prompt text NOT NULL, scope text NOT NULL, answer_fact text REFERENCES facts(id),
 UNIQUE(application_id,key)
);
CREATE TABLE IF NOT EXISTS jobs (
 id text PRIMARY KEY, kind text NOT NULL, status text NOT NULL DEFAULT 'queued',
 input jsonb NOT NULL, checkpoint jsonb NOT NULL DEFAULT '{}', result jsonb NOT NULL DEFAULT '{}',
 attempts integer NOT NULL DEFAULT 0, max_attempts integer NOT NULL DEFAULT 3,
 cancel_requested boolean NOT NULL DEFAULT false, lease_until timestamptz,
 available_at timestamptz NOT NULL DEFAULT now(), worker text, error text,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status,available_at);
CREATE TABLE IF NOT EXISTS tracker_snapshots (
 id text PRIMARY KEY, candidate text NOT NULL, path text NOT NULL, hash text NOT NULL,
 records jsonb NOT NULL, totals jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS conflicts (
 id text PRIMARY KEY, kind text NOT NULL, subject text NOT NULL, detail jsonb NOT NULL,
 resolved boolean NOT NULL DEFAULT false, resolution jsonb
);
CREATE TABLE IF NOT EXISTS followups (
 id text PRIMARY KEY, application_id text REFERENCES applications(id), candidate text NOT NULL,
 category text NOT NULL, detail jsonb NOT NULL, resolved boolean NOT NULL DEFAULT false
);
CREATE TABLE IF NOT EXISTS reports (
 fingerprint text PRIMARY KEY, path text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS audit (
 seq bigserial PRIMARY KEY, at timestamptz NOT NULL DEFAULT now(), event text NOT NULL,
 subject text NOT NULL, detail jsonb NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS metrics (
 name text PRIMARY KEY, value double precision NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS settings (key text PRIMARY KEY, value jsonb NOT NULL);
