-- CommAI voice stage 5 (ADR 0033): real numbers by country, porting, emergency
-- addresses per island, several carriers with routing and trunk health, and
-- international revenue share fraud protection. Applied after 50_voice.sql,
-- idempotently. Business records carry customer_id; carriers, their rates and
-- the high-risk list are ExaCarib's own (platform) records.

-- ---- numbers by country -------------------------------------------------------------

ALTER TABLE voice_numbers ADD COLUMN IF NOT EXISTS country text NOT NULL DEFAULT '';
-- Numbers that already existed keep calling out; new ones start blocked until
-- the emergency address rules for their island are met (see emergency.py).
ALTER TABLE voice_numbers ADD COLUMN IF NOT EXISTS outbound_enabled boolean NOT NULL DEFAULT true;
ALTER TABLE voice_numbers ALTER COLUMN outbound_enabled SET DEFAULT false;
ALTER TABLE voice_numbers ADD COLUMN IF NOT EXISTS outbound_reason text NOT NULL DEFAULT '';
ALTER TABLE voice_numbers ADD COLUMN IF NOT EXISTS carrier text NOT NULL DEFAULT '';

ALTER TABLE voice_sim_provider_numbers ADD COLUMN IF NOT EXISTS country text NOT NULL DEFAULT 'TT';

-- The simulated provider's long-running requests (ports, address checks):
-- each one becomes ready at ready_at, the way a real provider answers later.
CREATE TABLE IF NOT EXISTS voice_sim_provider_requests (
  ref          text PRIMARY KEY,
  kind         text NOT NULL,              -- port | emergency
  customer_id  uuid,
  payload      jsonb NOT NULL DEFAULT '{}',
  status       text NOT NULL DEFAULT 'pending',
  outcome      jsonb NOT NULL DEFAULT '{}',
  ready_at     timestamptz NOT NULL DEFAULT now(),
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now()
);

-- ---- emergency addresses -------------------------------------------------------------

ALTER TABLE voice_sites ADD COLUMN IF NOT EXISTS emergency_ref text NOT NULL DEFAULT '';
ALTER TABLE voice_sites ADD COLUMN IF NOT EXISTS emergency_reason text NOT NULL DEFAULT '';
ALTER TABLE voice_sites ADD COLUMN IF NOT EXISTS emergency_checked_at timestamptz;
ALTER TABLE voice_sites ADD COLUMN IF NOT EXISTS emergency_normalised jsonb NOT NULL DEFAULT '{}';

-- A person normally uses their site's address. emergency_own = true means they
-- have their own (a home worker), validated on its own.
ALTER TABLE voice_users ADD COLUMN IF NOT EXISTS emergency_own boolean NOT NULL DEFAULT false;
ALTER TABLE voice_users ADD COLUMN IF NOT EXISTS emergency_status text NOT NULL DEFAULT 'not_registered';
ALTER TABLE voice_users ADD COLUMN IF NOT EXISTS emergency_ref text NOT NULL DEFAULT '';
ALTER TABLE voice_users ADD COLUMN IF NOT EXISTS emergency_reason text NOT NULL DEFAULT '';

-- Each person acknowledges the emergency calling notice for their island.
CREATE TABLE IF NOT EXISTS voice_emergency_notices (
  customer_id      uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  voice_user_id    uuid NOT NULL REFERENCES voice_users(id) ON DELETE CASCADE,
  notice_key       text NOT NULL,          -- country + hash of the text shown
  text             text NOT NULL,
  acknowledged_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (voice_user_id, notice_key)
);

-- In-portal notices for a business's voice admins (or one person).
CREATE TABLE IF NOT EXISTS voice_notifications (
  id             bigserial PRIMARY KEY,
  customer_id    uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  voice_user_id  uuid REFERENCES voice_users(id) ON DELETE CASCADE,
  kind           text NOT NULL,
  title          text NOT NULL,
  body           text NOT NULL DEFAULT '',
  subject        text NOT NULL DEFAULT '',
  at             timestamptz NOT NULL DEFAULT now(),
  read_at        timestamptz
);
CREATE INDEX IF NOT EXISTS voice_notifications_customer ON voice_notifications (customer_id, at DESC);

-- ---- porting -----------------------------------------------------------------------

ALTER TABLE voice_port_orders DROP CONSTRAINT IF EXISTS voice_port_orders_status_check;
ALTER TABLE voice_port_orders ADD CONSTRAINT voice_port_orders_status_check CHECK (status IN (
  'draft', 'submitted', 'documents_needed', 'accepted', 'scheduled', 'cutting_over',
  'completed', 'rolled_back', 'rejected', 'cancelled'));
ALTER TABLE voice_port_orders ADD COLUMN IF NOT EXISTS country text NOT NULL DEFAULT '';
ALTER TABLE voice_port_orders ADD COLUMN IF NOT EXISTS account_name text NOT NULL DEFAULT '';
ALTER TABLE voice_port_orders ADD COLUMN IF NOT EXISTS account_number text NOT NULL DEFAULT '';
ALTER TABLE voice_port_orders ADD COLUMN IF NOT EXISTS service_address jsonb NOT NULL DEFAULT '{}';
ALTER TABLE voice_port_orders ADD COLUMN IF NOT EXISTS requested_date date;
ALTER TABLE voice_port_orders ADD COLUMN IF NOT EXISTS foc_date date;            -- firm order commitment
ALTER TABLE voice_port_orders ADD COLUMN IF NOT EXISTS cutover_at timestamptz;
ALTER TABLE voice_port_orders ADD COLUMN IF NOT EXISTS rejection_code text NOT NULL DEFAULT '';
ALTER TABLE voice_port_orders ADD COLUMN IF NOT EXISTS rejection_reason text NOT NULL DEFAULT '';
ALTER TABLE voice_port_orders ADD COLUMN IF NOT EXISTS created_by text NOT NULL DEFAULT '';
ALTER TABLE voice_port_orders ADD COLUMN IF NOT EXISTS target_type text NOT NULL DEFAULT 'none';
ALTER TABLE voice_port_orders ADD COLUMN IF NOT EXISTS target_id uuid;
ALTER TABLE voice_port_orders ADD COLUMN IF NOT EXISTS site_id uuid;

CREATE TABLE IF NOT EXISTS voice_port_events (
  id           bigserial PRIMARY KEY,
  port_id      uuid NOT NULL REFERENCES voice_port_orders(id) ON DELETE CASCADE,
  customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  status       text NOT NULL,
  detail       text NOT NULL DEFAULT '',
  actor        text NOT NULL DEFAULT '',
  at           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS voice_port_events_port ON voice_port_events (port_id, id);

-- Port documents. The file lives outside the database under the data
-- directory, named by a random id; only the checked metadata is here.
CREATE TABLE IF NOT EXISTS voice_port_documents (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id      uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  port_id          uuid NOT NULL REFERENCES voice_port_orders(id) ON DELETE CASCADE,
  doc_type         text NOT NULL CHECK (doc_type IN ('loa', 'bill', 'id', 'other')),
  filename         text NOT NULL,
  content_type     text NOT NULL,
  size_bytes       int NOT NULL,
  sha256           text NOT NULL,
  storage_key      text NOT NULL,
  uploaded_by      text NOT NULL,
  uploaded_at      timestamptz NOT NULL DEFAULT now(),
  removed_at       timestamptz
);
CREATE INDEX IF NOT EXISTS voice_port_documents_port ON voice_port_documents (port_id);

-- ---- carriers (ExaCarib's SIP suppliers) -----------------------------------------------

CREATE TABLE IF NOT EXISTS voice_carriers (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  key                   text NOT NULL UNIQUE,      -- also the go-live key (kind 'carrier')
  name                  text NOT NULL,
  adapter               text NOT NULL DEFAULT 'simulated' CHECK (adapter IN ('simulated', 'sip')),
  countries             text[] NOT NULL DEFAULT '{}',
  outbound              jsonb NOT NULL DEFAULT '{}',  -- host, port, transport, codecs, max_channels, prefix
  inbound               jsonb NOT NULL DEFAULT '{}',  -- allow_ips (the carrier's signalling addresses)
  enabled               boolean NOT NULL DEFAULT true,
  health                text NOT NULL DEFAULT 'unknown' CHECK (health IN ('unknown', 'up', 'degraded', 'down')),
  consecutive_failures  int NOT NULL DEFAULT 0,
  last_options_at       timestamptz,
  last_rtt_ms           int,
  rate_version          int NOT NULL DEFAULT 0,
  created_by            text NOT NULL DEFAULT '',
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now()
);

-- What each carrier charges ExaCarib per minute, by destination prefix. Versioned.
CREATE TABLE IF NOT EXISTS voice_carrier_rates (
  carrier_id   uuid NOT NULL REFERENCES voice_carriers(id) ON DELETE CASCADE,
  version      int NOT NULL,
  prefix       text NOT NULL,
  name         text NOT NULL DEFAULT '',
  per_minute   numeric(12, 6) NOT NULL,
  created_by   text NOT NULL DEFAULT '',
  created_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (carrier_id, version, prefix)
);

-- SIP OPTIONS results (kept for a week).
CREATE TABLE IF NOT EXISTS voice_carrier_options (
  id          bigserial PRIMARY KEY,
  carrier_id  uuid NOT NULL REFERENCES voice_carriers(id) ON DELETE CASCADE,
  ok          boolean NOT NULL,
  rtt_ms      int,
  code        int NOT NULL DEFAULT 0,
  at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS voice_carrier_options_recent ON voice_carrier_options (carrier_id, at DESC);

-- Routing mode per destination prefix: least cost first, or best quality first.
CREATE TABLE IF NOT EXISTS voice_route_rules (
  prefix      text PRIMARY KEY,                  -- '' is the default for every destination
  mode        text NOT NULL CHECK (mode IN ('lcr', 'quality')),
  updated_by  text NOT NULL DEFAULT '',
  updated_at  timestamptz NOT NULL DEFAULT now()
);
INSERT INTO voice_route_rules (prefix, mode, updated_by) VALUES ('', 'lcr', 'system') ON CONFLICT DO NOTHING;

-- Which carriers a call tried, in order (failover is visible per call).
CREATE TABLE IF NOT EXISTS voice_route_attempts (
  id           bigserial PRIMARY KEY,
  customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  call_id      text NOT NULL,
  position     int NOT NULL,
  carrier      text NOT NULL,
  ok           boolean NOT NULL,
  detail       text NOT NULL DEFAULT '',
  at           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS voice_route_attempts_call ON voice_route_attempts (customer_id, call_id);

ALTER TABLE voice_cdrs ADD COLUMN IF NOT EXISTS carrier text NOT NULL DEFAULT '';
ALTER TABLE voice_cdrs ADD COLUMN IF NOT EXISTS carrier_cost numeric(14, 4);
ALTER TABLE voice_cdrs ADD COLUMN IF NOT EXISTS international boolean NOT NULL DEFAULT false;

-- ---- international revenue share fraud ----------------------------------------------

ALTER TABLE voice_fraud_limits ADD COLUMN IF NOT EXISTS intl_daily_cap numeric(12, 2);
ALTER TABLE voice_fraud_limits ADD COLUMN IF NOT EXISTS intl_daily_calls int NOT NULL DEFAULT 200;
ALTER TABLE voice_fraud_limits ADD COLUMN IF NOT EXISTS after_hours_international text NOT NULL DEFAULT 'alert';
ALTER TABLE voice_fraud_limits ADD COLUMN IF NOT EXISTS hours_id uuid;
ALTER TABLE voice_fraud_limits ADD COLUMN IF NOT EXISTS spike_factor numeric(6, 2) NOT NULL DEFAULT 5;
ALTER TABLE voice_fraud_limits ADD COLUMN IF NOT EXISTS spike_min_calls int NOT NULL DEFAULT 20;
ALTER TABLE voice_fraud_limits ADD COLUMN IF NOT EXISTS allowed_high_risk text[] NOT NULL DEFAULT '{}';
ALTER TABLE voice_fraud_limits ADD COLUMN IF NOT EXISTS intl_suspended boolean NOT NULL DEFAULT false;
ALTER TABLE voice_fraud_limits ADD COLUMN IF NOT EXISTS suspended_at timestamptz;
ALTER TABLE voice_fraud_limits ADD COLUMN IF NOT EXISTS suspended_reason text NOT NULL DEFAULT '';
ALTER TABLE voice_fraud_limits ADD COLUMN IF NOT EXISTS restored_by text NOT NULL DEFAULT '';
ALTER TABLE voice_fraud_limits ADD COLUMN IF NOT EXISTS restored_at timestamptz;

-- High-risk destinations for revenue share fraud, per calling country ('*' for
-- every country). ExaCarib keeps the list; a business can allow a prefix it needs.
CREATE TABLE IF NOT EXISTS voice_high_risk_destinations (
  origin      text NOT NULL DEFAULT '*',
  prefix      text NOT NULL,
  name        text NOT NULL DEFAULT '',
  reason      text NOT NULL DEFAULT '',
  created_by  text NOT NULL DEFAULT 'system',
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (origin, prefix)
);

-- A starting list, seeded once into an empty table. ExaCarib reviews it with
-- each carrier's fraud team; it is shown as an example list until then.
INSERT INTO voice_high_risk_destinations (origin, prefix, name, reason)
SELECT origin, prefix, name, reason FROM (VALUES
  ('*', '53',   'Cuba', 'High termination rates, often used for revenue share fraud'),
  ('*', '252',  'Somalia', 'Often used for revenue share fraud'),
  ('*', '232',  'Sierra Leone', 'Often used for revenue share fraud'),
  ('*', '224',  'Guinea', 'Often used for revenue share fraud'),
  ('*', '220',  'Gambia', 'Often used for revenue share fraud'),
  ('*', '231',  'Liberia', 'Often used for revenue share fraud'),
  ('*', '239',  'Sao Tome and Principe', 'Often used for revenue share fraud'),
  ('*', '371',  'Latvia', 'Premium ranges often used for revenue share fraud'),
  ('*', '370',  'Lithuania', 'Premium ranges often used for revenue share fraud'),
  ('*', '246',  'Diego Garcia', 'Remote territory with high termination rates'),
  ('*', '674',  'Nauru', 'Remote territory with high termination rates'),
  ('*', '675',  'Papua New Guinea', 'Often used for revenue share fraud'),
  ('*', '677',  'Solomon Islands', 'Remote territory with high termination rates'),
  ('*', '682',  'Cook Islands', 'Remote territory with high termination rates'),
  ('*', '683',  'Niue', 'Remote territory with high termination rates'),
  ('*', '688',  'Tuvalu', 'Remote territory with high termination rates'),
  ('*', '690',  'Tokelau', 'Remote territory with high termination rates'),
  ('*', '870',  'Inmarsat', 'Satellite numbers with very high rates'),
  ('US', '1809', 'Dominican Republic', 'One-ring call-back scams aimed at North American callers'),
  ('US', '1829', 'Dominican Republic', 'One-ring call-back scams aimed at North American callers'),
  ('US', '1849', 'Dominican Republic', 'One-ring call-back scams aimed at North American callers'),
  ('US', '1473', 'Grenada', 'One-ring call-back scams aimed at North American callers'),
  ('US', '1268', 'Antigua and Barbuda', 'One-ring call-back scams aimed at North American callers'),
  ('US', '1284', 'British Virgin Islands', 'One-ring call-back scams aimed at North American callers'),
  ('US', '1649', 'Turks and Caicos', 'One-ring call-back scams aimed at North American callers'),
  ('US', '1876', 'Jamaica', 'One-ring call-back scams aimed at North American callers'),
  ('CA', '1809', 'Dominican Republic', 'One-ring call-back scams aimed at North American callers'),
  ('CA', '1473', 'Grenada', 'One-ring call-back scams aimed at North American callers'),
  ('CA', '1876', 'Jamaica', 'One-ring call-back scams aimed at North American callers'),
  ('GB', '882',  'International networks', 'Shared-cost international ranges often abused')
) AS seed (origin, prefix, name, reason)
WHERE NOT EXISTS (SELECT 1 FROM voice_high_risk_destinations);
