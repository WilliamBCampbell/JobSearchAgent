ALTER TABLE answer_cache ADD COLUMN IF NOT EXISTS request_key text;
CREATE INDEX IF NOT EXISTS answer_cache_request ON answer_cache(request_key) WHERE valid;
UPDATE answer_cache SET valid=false WHERE request_key IS NULL;
