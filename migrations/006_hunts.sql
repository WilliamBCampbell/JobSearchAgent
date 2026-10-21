CREATE TABLE hunts (
 id text PRIMARY KEY, candidate text NOT NULL, status text NOT NULL DEFAULT 'running',
 request jsonb NOT NULL, checkpoint jsonb NOT NULL DEFAULT '{}',
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE hunt_leads (
 id text PRIMARY KEY, hunt_id text NOT NULL REFERENCES hunts(id), ordinal integer NOT NULL,
 discovery jsonb NOT NULL, status text NOT NULL DEFAULT 'discovered', reason text,
 application_id text REFERENCES applications(id), child_job text REFERENCES jobs(id),
 evidence jsonb NOT NULL DEFAULT '{}', updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(hunt_id,ordinal)
);
CREATE INDEX hunt_leads_run ON hunt_leads(hunt_id,status);
