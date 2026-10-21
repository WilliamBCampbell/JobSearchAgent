ALTER TABLE chunks ADD COLUMN IF NOT EXISTS historical boolean NOT NULL DEFAULT false;
CREATE INDEX IF NOT EXISTS chunks_current_sections ON chunks(version_id) WHERE NOT historical;
