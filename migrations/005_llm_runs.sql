CREATE TABLE llm_runs (
 id text PRIMARY KEY, job_id text REFERENCES jobs(id), task text NOT NULL,
 candidate text NOT NULL, application_id text REFERENCES applications(id),
 fingerprint text NOT NULL, backend text NOT NULL, model text NOT NULL, effort text NOT NULL,
 status text NOT NULL, input jsonb NOT NULL, output jsonb, error text,
 usage jsonb NOT NULL DEFAULT '{}', directory text, pid integer,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX llm_runs_cache ON llm_runs(fingerprint) WHERE status='complete';
CREATE TABLE llm_events (
 seq bigserial PRIMARY KEY, run_id text NOT NULL REFERENCES llm_runs(id),
 at timestamptz NOT NULL DEFAULT now(), event text NOT NULL, detail jsonb NOT NULL DEFAULT '{}'
);
