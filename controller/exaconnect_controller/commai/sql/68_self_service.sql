-- Jibsy self-service (ADR 0037): "My settings" for staff and the help centre
-- for a business's own customers. Idempotent; applied at startup.

-- A person's own Jibsy preferences, per business. Only that person changes
-- them (the API takes the person from the session, never from the request).
CREATE TABLE IF NOT EXISTS ss_staff_prefs (
  customer_id    uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  user_id        uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  language       text NOT NULL DEFAULT 'en',
  availability   text NOT NULL DEFAULT 'online' CHECK (availability IN ('online', 'away', 'offline')),
  -- {"assignment": {"in_app": true, "email": false}, "mention": {...}, "sla_warning": {...}}
  notify         jsonb NOT NULL DEFAULT '{}',
  quiet_start    text NOT NULL DEFAULT '',      -- "22:00", empty for none
  quiet_end      text NOT NULL DEFAULT '',
  timezone       text NOT NULL DEFAULT '',      -- empty: the business's time zone
  updated_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (customer_id, user_id)
);

-- The help centre: one per business, off until a business admin switches it on.
CREATE TABLE IF NOT EXISTS help_centres (
  customer_id  uuid PRIMARY KEY REFERENCES customers(id) ON DELETE CASCADE,
  slug         text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9][a-z0-9-]{1,48}[a-z0-9]$'),
  enabled      boolean NOT NULL DEFAULT false,
  settings     jsonb NOT NULL DEFAULT '{}',
  updated_by   text NOT NULL DEFAULT '',
  updated_at   timestamptz NOT NULL DEFAULT now()
);

-- Which approved knowledge sources are published as help articles. Publishing
-- is a separate decision from approving a source for the AI: a person must
-- publish it, and editing the source after that hides it until published again
-- (published_at must be later than the source's updated_at).
CREATE TABLE IF NOT EXISTS help_articles (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id   uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  source_id     uuid NOT NULL REFERENCES knowledge_sources(id) ON DELETE CASCADE,
  category      text NOT NULL DEFAULT 'General',
  position      int NOT NULL DEFAULT 100,
  published     boolean NOT NULL DEFAULT false,
  published_by  text NOT NULL DEFAULT '',
  published_at  timestamptz,
  UNIQUE (customer_id, source_id)
);

-- Emailed one-time sign-in links for end users. Only the hash is kept.
CREATE TABLE IF NOT EXISTS ss_magic_links (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  token_hash   text NOT NULL UNIQUE,
  identity_id  uuid NOT NULL REFERENCES contact_identities(id) ON DELETE CASCADE,
  expires_at   timestamptz NOT NULL,
  used_at      timestamptz,
  created_at   timestamptz NOT NULL DEFAULT now()
);

-- Public help-centre requests (sign-in links, Ask, contact form) for rate
-- limits. key_hash: a hash of the email address or client address.
CREATE TABLE IF NOT EXISTS ss_public_hits (
  id           bigserial PRIMARY KEY,
  customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  bucket       text NOT NULL,
  key_hash     text NOT NULL,
  at           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ss_public_hits_key ON ss_public_hits (customer_id, bucket, key_hash, at);

-- End-user sessions on the help centre. identity_ids: the addresses this
-- person proved (the email they clicked from, or the business's signed token).
CREATE TABLE IF NOT EXISTS ss_enduser_sessions (
  token_hash    text PRIMARY KEY,
  customer_id   uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  contact_id    uuid NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
  identity_ids  uuid[] NOT NULL,
  via           text NOT NULL CHECK (via IN ('magic_link', 'business_token')),
  expires_at    timestamptz NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now()
);

-- Booking changes an end user asked for, carried out through commai.actions.
CREATE TABLE IF NOT EXISTS ss_booking_changes (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id     uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  contact_id      uuid NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
  booking_run_id  uuid NOT NULL REFERENCES action_runs(id) ON DELETE CASCADE,
  kind            text NOT NULL CHECK (kind IN ('reschedule', 'cancel')),
  new_start       text NOT NULL DEFAULT '',
  book_run_id     uuid REFERENCES action_runs(id) ON DELETE SET NULL,
  cancel_run_id   uuid REFERENCES action_runs(id) ON DELETE SET NULL,
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ss_booking_changes_booking ON ss_booking_changes (booking_run_id);

-- "Download my data" and "delete my data" requests for the business admin.
CREATE TABLE IF NOT EXISTS ss_data_requests (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id   uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  contact_id    uuid REFERENCES contacts(id) ON DELETE SET NULL,
  kind          text NOT NULL CHECK (kind IN ('download', 'delete')),
  status        text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done', 'refused')),
  detail        text NOT NULL DEFAULT '',
  handled_by    text NOT NULL DEFAULT '',
  handled_at    timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ss_data_requests_customer ON ss_data_requests (customer_id, status, created_at DESC);
-- Handled through data governance (ADR 0030): the subject request it became, and why it was refused.
ALTER TABLE ss_data_requests ADD COLUMN IF NOT EXISTS subject_request_id uuid;
ALTER TABLE ss_data_requests ADD COLUMN IF NOT EXISTS reason text NOT NULL DEFAULT '';
