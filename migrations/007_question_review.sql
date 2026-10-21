-- Preserve the prior answer while a user reviews a correction, including across restarts.
ALTER TABLE questions ADD COLUMN IF NOT EXISTS needs_review boolean NOT NULL DEFAULT false;
