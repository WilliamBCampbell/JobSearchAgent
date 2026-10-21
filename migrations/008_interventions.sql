ALTER TABLE questions ADD COLUMN reason jsonb NOT NULL DEFAULT '{}';

CREATE TABLE notifications (
 id text PRIMARY KEY,
 application_id text NOT NULL REFERENCES applications(id),
 kind text NOT NULL,
 detail jsonb NOT NULL,
 fingerprint text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now(),
 read_at timestamptz,
 resolved_at timestamptz,
 UNIQUE(application_id,kind)
);
CREATE INDEX notifications_active ON notifications(application_id) WHERE resolved_at IS NULL;
