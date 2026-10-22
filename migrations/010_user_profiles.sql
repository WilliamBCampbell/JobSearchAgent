-- Keep extension types/operators accessible without exposing another tenant's tables.
CREATE SCHEMA IF NOT EXISTS jsa_extensions;
ALTER EXTENSION vector SET SCHEMA jsa_extensions;
CREATE TABLE IF NOT EXISTS public.user_profiles (
 id text PRIMARY KEY,
 name text NOT NULL CHECK (length(trim(name)) > 0),
 workspace text NOT NULL,
 schema_name text NOT NULL UNIQUE,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS user_profiles_name ON public.user_profiles (lower(name));
