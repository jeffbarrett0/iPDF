CREATE TABLE IF NOT EXISTS files (
  id uuid PRIMARY KEY,
  kind text NOT NULL CHECK (kind IN ('original', 'output')),
  root_id uuid NOT NULL,
  version int NOT NULL,
  display_name text NOT NULL,
  stored_path text,
  mime text NOT NULL,
  sha256 text NOT NULL,
  size_bytes bigint NOT NULL,
  page_count int,
  operation text,
  params jsonb NOT NULL DEFAULT '{}',
  source_ids uuid[] NOT NULL DEFAULT '{}',
  analysis jsonb NOT NULL DEFAULT '{}',
  warnings jsonb NOT NULL DEFAULT '[]',
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz,
  deleted_at timestamptz,
  deleted_reason text
);
CREATE INDEX IF NOT EXISTS files_root_idx ON files (root_id, version);

-- Content is immutable: only expiry and tombstoning may change after insert.
CREATE OR REPLACE FUNCTION files_guard() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'files rows are never deleted; they are tombstoned';
  END IF;
  IF NEW.id <> OLD.id OR NEW.kind <> OLD.kind OR NEW.root_id <> OLD.root_id
     OR NEW.version <> OLD.version OR NEW.sha256 <> OLD.sha256
     OR NEW.size_bytes <> OLD.size_bytes OR NEW.operation IS DISTINCT FROM OLD.operation
     OR NEW.params <> OLD.params OR NEW.source_ids <> OLD.source_ids
     OR NEW.created_at <> OLD.created_at OR NEW.display_name <> OLD.display_name THEN
    RAISE EXCEPTION 'file records are immutable';
  END IF;
  IF NEW.stored_path IS DISTINCT FROM OLD.stored_path AND NEW.stored_path IS NOT NULL THEN
    RAISE EXCEPTION 'file records are immutable';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS files_guard_trg ON files;
CREATE TRIGGER files_guard_trg BEFORE UPDATE OR DELETE ON files
  FOR EACH ROW EXECUTE FUNCTION files_guard();

CREATE TABLE IF NOT EXISTS events (
  id bigserial PRIMARY KEY,
  ts timestamptz NOT NULL,
  type text NOT NULL,
  file_id uuid,
  sha256 text,
  outcome text NOT NULL DEFAULT 'ok',
  details jsonb NOT NULL DEFAULT '{}',
  prev_hash text NOT NULL,
  hash text NOT NULL
);
CREATE INDEX IF NOT EXISTS events_file_idx ON events (file_id);

CREATE OR REPLACE FUNCTION events_append_only() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'the event log is append-only';
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS events_no_change ON events;
CREATE TRIGGER events_no_change BEFORE UPDATE OR DELETE ON events
  FOR EACH ROW EXECUTE FUNCTION events_append_only();
DROP TRIGGER IF EXISTS events_no_truncate ON events;
CREATE TRIGGER events_no_truncate BEFORE TRUNCATE ON events
  FOR EACH STATEMENT EXECUTE FUNCTION events_append_only();

CREATE TABLE IF NOT EXISTS sign_requests (
  id uuid PRIMARY KEY,
  file_id uuid NOT NULL REFERENCES files(id),
  title text NOT NULL,
  message text NOT NULL DEFAULT '',
  status text NOT NULL DEFAULT 'draft'
    CHECK (status IN ('draft','sent','completed','sealed','voided')),
  source_sha256 text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  sent_at timestamptz,
  completed_at timestamptz,
  voided_at timestamptz,
  sealed_file_id uuid REFERENCES files(id),
  sealed_sha256 text
);

CREATE TABLE IF NOT EXISTS signers (
  id uuid PRIMARY KEY,
  request_id uuid NOT NULL REFERENCES sign_requests(id),
  name text NOT NULL,
  email text NOT NULL,
  token_hash text UNIQUE,
  token_expires_at timestamptz,
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','invited','viewed','consented','signed','declined')),
  invited_at timestamptz,
  viewed_at timestamptz,
  consent_at timestamptz,
  consent_text_sha256 text,
  consent_ip text,
  consent_user_agent text,
  signed_at timestamptz,
  decline_reason text
);

CREATE TABLE IF NOT EXISTS sign_fields (
  id uuid PRIMARY KEY,
  request_id uuid NOT NULL REFERENCES sign_requests(id),
  signer_id uuid NOT NULL REFERENCES signers(id),
  page int NOT NULL CHECK (page >= 1),
  x real NOT NULL, y real NOT NULL, w real NOT NULL, h real NOT NULL,
  kind text NOT NULL CHECK (kind IN ('signature','date','text')),
  label text NOT NULL DEFAULT '',
  required boolean NOT NULL DEFAULT true,
  value jsonb,
  filled_at timestamptz
);
