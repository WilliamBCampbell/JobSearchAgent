CREATE TABLE requirement_decisions (
 id text PRIMARY KEY,
 application_id text NOT NULL REFERENCES applications(id),
 requirement_key text NOT NULL,
 requirement jsonb NOT NULL,
 decision text NOT NULL CHECK (decision IN ('confirm','deny')),
 note text NOT NULL DEFAULT '',
 fact_id text NOT NULL REFERENCES facts(id),
 score_hash text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX requirement_decisions_application ON requirement_decisions(application_id,requirement_key);
