-- Enterprise administration and data governance (ADR 0030). Idempotent;
-- applied after the earlier CommAI files. Every record carries customer_id.

-- ---- Organisation: locations, brands, business calendars --------------------

CREATE TABLE IF NOT EXISTS commai_locations (
  id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id          uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  name                 text NOT NULL,
  country              text NOT NULL DEFAULT '',          -- ISO 3166-1 alpha-2, for public holidays
  timezone             text NOT NULL DEFAULT 'America/Port_of_Spain',
  address              text NOT NULL DEFAULT '',
  is_primary           boolean NOT NULL DEFAULT false,
  -- Where conversations go while this location is closed (NULL: they wait in the team's queue).
  after_hours_team_id  uuid REFERENCES commai_teams(id) ON DELETE SET NULL,
  created_at           timestamptz NOT NULL DEFAULT now(),
  UNIQUE (customer_id, name)
);
CREATE UNIQUE INDEX IF NOT EXISTS commai_locations_primary ON commai_locations (customer_id) WHERE is_primary;

CREATE TABLE IF NOT EXISTS commai_brands (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  name         text NOT NULL,
  location_id  uuid REFERENCES commai_locations(id) ON DELETE SET NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (customer_id, name)
);

-- Opening hours: one or more intervals per weekday (0 = Monday), local time.
-- A location with no rows is open all the time.
CREATE TABLE IF NOT EXISTS commai_opening_hours (
  location_id  uuid NOT NULL REFERENCES commai_locations(id) ON DELETE CASCADE,
  customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  weekday      int NOT NULL CHECK (weekday BETWEEN 0 AND 6),
  opens        time NOT NULL,
  closes       time NOT NULL CHECK (closes > opens),
  PRIMARY KEY (location_id, weekday, opens)
);

-- Public holidays per country, entered by the business (no outside calendar feed).
CREATE TABLE IF NOT EXISTS commai_holidays (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  country      text NOT NULL,
  day          date NOT NULL,
  name         text NOT NULL DEFAULT '',
  UNIQUE (customer_id, country, day)
);

-- One-off closures (a storm, a staff day). location_id NULL closes every location.
CREATE TABLE IF NOT EXISTS commai_closures (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  location_id  uuid REFERENCES commai_locations(id) ON DELETE CASCADE,
  starts_at    timestamptz NOT NULL,
  ends_at      timestamptz NOT NULL CHECK (ends_at > starts_at),
  reason       text NOT NULL DEFAULT '',
  created_by   text NOT NULL DEFAULT '',
  created_at   timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE commai_teams ADD COLUMN IF NOT EXISTS location_id uuid REFERENCES commai_locations(id) ON DELETE SET NULL;
ALTER TABLE commai_teams ADD COLUMN IF NOT EXISTS brand_id uuid REFERENCES commai_brands(id) ON DELETE SET NULL;

-- ---- Roles -------------------------------------------------------------------

-- customer_id NULL: a built-in role (the same for every business).
CREATE TABLE IF NOT EXISTS commai_roles (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id  uuid REFERENCES customers(id) ON DELETE CASCADE,
  key          text NOT NULL,
  name         text NOT NULL,
  description  text NOT NULL DEFAULT '',
  permissions  text[] NOT NULL DEFAULT '{}',
  created_by   text NOT NULL DEFAULT '',
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS commai_roles_builtin ON commai_roles (key) WHERE customer_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS commai_roles_custom ON commai_roles (customer_id, key) WHERE customer_id IS NOT NULL;

INSERT INTO commai_roles (customer_id, key, name, description, permissions)
SELECT NULL, v.key, v.name, v.description, v.permissions FROM (VALUES
  ('business_admin', 'Business admin', 'Everything, including security and data.',
   ARRAY['read_inbox','reply','notes','manage_teams','manage_channels','manage_integrations','approve_spending',
         'voice_admin','view_reports','export_data','manage_security']),
  ('agent', 'Agent', 'Answers customers, writes notes and sees reports. The agent seat.',
   ARRAY['read_inbox','reply','notes','view_reports']),
  ('internal', 'Internal', 'Reads conversations and writes private notes. Never replies. The internal seat.',
   ARRAY['read_inbox','notes']),
  ('viewer', 'Viewer', 'Reads conversations and reports only.', ARRAY['read_inbox','view_reports'])
) AS v(key, name, description, permissions)
WHERE NOT EXISTS (SELECT 1 FROM commai_roles r WHERE r.customer_id IS NULL AND r.key = v.key);

-- A person's roles in a business. team_id NULL: across the business; otherwise
-- only for that team's conversations. A person with no rows keeps the rights
-- of their account and seat, exactly as before roles existed.
CREATE TABLE IF NOT EXISTS commai_role_assignments (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  user_id      uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role_id      uuid NOT NULL REFERENCES commai_roles(id) ON DELETE CASCADE,
  team_id      uuid REFERENCES commai_teams(id) ON DELETE CASCADE,
  created_by   text NOT NULL DEFAULT '',
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS commai_role_assignments_one
  ON commai_role_assignments (customer_id, user_id, role_id, COALESCE(team_id, '00000000-0000-0000-0000-000000000000'::uuid));
CREATE INDEX IF NOT EXISTS commai_role_assignments_user ON commai_role_assignments (user_id, customer_id);

-- ---- Security settings per business -------------------------------------------

CREATE TABLE IF NOT EXISTS commai_security_settings (
  customer_id       uuid PRIMARY KEY REFERENCES customers(id) ON DELETE CASCADE,
  require_two_step  boolean NOT NULL DEFAULT false,
  session_hours     int CHECK (session_hours IS NULL OR session_hours BETWEEN 1 AND 720),
  ip_allowlist      text[] NOT NULL DEFAULT '{}',        -- CIDRs; empty: any address
  sso_logout        boolean NOT NULL DEFAULT true,       -- sign-out also signs out of the company's provider
  thresholds        jsonb NOT NULL DEFAULT '{}',          -- unusual-use thresholds that differ from the defaults
  updated_by        text NOT NULL DEFAULT '',
  updated_at        timestamptz NOT NULL DEFAULT now()
);

-- The gateway's ID token for an SSO session, so sign-out can reach the provider.
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS id_token text;

-- API key protection: a temporary lock on clear abuse, lifted by an admin or by time.
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS locked_until timestamptz;
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS locked_reason text NOT NULL DEFAULT '';

-- Where each API key has been used from (updated at most once a minute per address).
CREATE TABLE IF NOT EXISTS commai_api_key_sightings (
  key_id         bigint NOT NULL REFERENCES api_keys(id) ON DELETE CASCADE,
  customer_id    uuid,
  ip             text NOT NULL,
  country        text NOT NULL DEFAULT '',
  first_seen_at  timestamptz NOT NULL DEFAULT now(),
  last_seen_at   timestamptz NOT NULL DEFAULT now(),
  hits           bigint NOT NULL DEFAULT 1,
  PRIMARY KEY (key_id, ip)
);

-- Unusual use, raised to the business (Security tab, webhook) and to ExaCarib.
CREATE TABLE IF NOT EXISTS commai_security_alerts (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  kind         text NOT NULL,
  severity     text NOT NULL DEFAULT 'warning' CHECK (severity IN ('info', 'warning', 'critical')),
  summary      text NOT NULL,
  evidence     jsonb NOT NULL DEFAULT '{}',
  action_taken text NOT NULL DEFAULT '',
  dedupe_key   text NOT NULL,
  status       text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'acknowledged')),
  acknowledged_by text NOT NULL DEFAULT '',
  acknowledged_at timestamptz,
  created_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (customer_id, dedupe_key)
);
CREATE INDEX IF NOT EXISTS commai_security_alerts_open ON commai_security_alerts (status, created_at DESC);

-- ---- Data governance -----------------------------------------------------------

-- days NULL: keep. Categories: messages, notes, call_recordings, transcripts, ai_logs.
CREATE TABLE IF NOT EXISTS commai_retention_rules (
  customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  category     text NOT NULL CHECK (category IN ('messages', 'notes', 'call_recordings', 'transcripts', 'ai_logs')),
  days         int CHECK (days IS NULL OR days BETWEEN 1 AND 36500),
  updated_by   text NOT NULL DEFAULT '',
  updated_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (customer_id, category)
);
CREATE TABLE IF NOT EXISTS commai_data_settings (
  customer_id  uuid PRIMARY KEY REFERENCES customers(id) ON DELETE CASCADE,
  hold_all     boolean NOT NULL DEFAULT false,   -- a business-wide legal hold: nothing is deleted
  hold_reason  text NOT NULL DEFAULT '',
  updated_by   text NOT NULL DEFAULT '',
  updated_at   timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE contacts ADD COLUMN IF NOT EXISTS legal_hold boolean NOT NULL DEFAULT false;
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS legal_hold_reason text NOT NULL DEFAULT '';
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS anonymised_at timestamptz;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS redacted_at timestamptz;

-- Each run of the retention job: the cut-off per category and what it did.
CREATE TABLE IF NOT EXISTS commai_retention_runs (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  trigger      text NOT NULL DEFAULT 'schedule',
  cutoffs      jsonb NOT NULL DEFAULT '{}',
  counts       jsonb NOT NULL DEFAULT '{}',
  held         jsonb NOT NULL DEFAULT '{}',
  started_at   timestamptz NOT NULL DEFAULT now(),
  finished_at  timestamptz
);
CREATE INDEX IF NOT EXISTS commai_retention_runs_customer ON commai_retention_runs (customer_id, started_at DESC);

-- Subject requests (export or delete/anonymise one contact) and what was done.
CREATE TABLE IF NOT EXISTS commai_subject_requests (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id   uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  contact_id    uuid,
  kind          text NOT NULL CHECK (kind IN ('export', 'anonymise', 'delete')),
  status        text NOT NULL DEFAULT 'done' CHECK (status IN ('done', 'refused')),
  reason        text NOT NULL DEFAULT '',
  counts        jsonb NOT NULL DEFAULT '{}',
  requested_by  text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS commai_subject_requests_customer ON commai_subject_requests (customer_id, created_at DESC);

-- Business exports, built by a durable job and kept for seven days.
CREATE TABLE IF NOT EXISTS commai_exports (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  status       text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'ready', 'failed')),
  include_notes boolean NOT NULL DEFAULT true,
  data         bytea,
  size         bigint NOT NULL DEFAULT 0,
  counts       jsonb NOT NULL DEFAULT '{}',
  error        text NOT NULL DEFAULT '',
  requested_by text NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  ready_at     timestamptz,
  expires_at   timestamptz NOT NULL DEFAULT now() + interval '7 days'
);
CREATE INDEX IF NOT EXISTS commai_exports_customer ON commai_exports (customer_id, created_at DESC);
