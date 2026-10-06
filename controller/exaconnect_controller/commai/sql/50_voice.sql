-- CommAI voice (ADR 0021): phone system, provisioning and billing.
-- Applied after 00_core.sql, idempotently, at startup. Every record carries customer_id.
-- Money is numeric everywhere, never float.

-- Who in a business may change the phone system and approve spending.
-- With no rows for a business, every account of that business with the
-- commai:admin scope is a voice admin with spend permission (bootstrap).
CREATE TABLE IF NOT EXISTS voice_permissions (
  customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  user_id      uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  voice_admin  boolean NOT NULL DEFAULT true,
  spend        boolean NOT NULL DEFAULT false,
  PRIMARY KEY (customer_id, user_id)
);

-- Sites with their emergency (service) address. Island rules and provider
-- registration of the address are phase 3; the fields are ready for them.
CREATE TABLE IF NOT EXISTS voice_sites (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id       uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  name              text NOT NULL,
  timezone          text NOT NULL DEFAULT 'America/Port_of_Spain',
  address_line1     text NOT NULL DEFAULT '',
  address_line2     text NOT NULL DEFAULT '',
  city              text NOT NULL DEFAULT '',
  island            text NOT NULL DEFAULT '',
  country           text NOT NULL DEFAULT 'TT',     -- ISO 3166-1 alpha-2
  postcode          text NOT NULL DEFAULT '',
  emergency_status  text NOT NULL DEFAULT 'not_registered'
                    CHECK (emergency_status IN ('not_registered', 'pending', 'registered', 'rejected')),
  created_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (customer_id, name)
);

-- Users and their extensions. user_id links to a portal account (self-service).
CREATE TABLE IF NOT EXISTS voice_users (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id         uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  user_id             uuid REFERENCES users(id) ON DELETE SET NULL,
  name                text NOT NULL,
  email               text NOT NULL DEFAULT '',
  mobile              text NOT NULL DEFAULT '',
  extension           text NOT NULL,
  site_id             uuid REFERENCES voice_sites(id) ON DELETE SET NULL,
  team_id             uuid REFERENCES commai_teams(id) ON DELETE SET NULL,
  status              text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'removed')),
  sip_password        text NOT NULL,
  forward_to          text NOT NULL DEFAULT '',
  forward_until       timestamptz,
  dnd                 boolean NOT NULL DEFAULT false,
  dnd_until           timestamptz,
  voicemail_greeting  text NOT NULL DEFAULT '',
  voicemail_to_email  boolean NOT NULL DEFAULT false,
  emergency_address   jsonb NOT NULL DEFAULT '{}',
  order_ref           text UNIQUE,
  billing_from        timestamptz,
  billing_until       timestamptz,
  created_at          timestamptz NOT NULL DEFAULT now(),
  removed_at          timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS voice_users_ext ON voice_users (customer_id, extension) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS voice_users_portal ON voice_users (customer_id, user_id);

-- Phone numbers (E.164). Ported numbers keep their port order's status.
CREATE TABLE IF NOT EXISTS voice_numbers (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id        uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  e164               text NOT NULL,
  source             text NOT NULL DEFAULT 'new' CHECK (source IN ('new', 'ported')),
  status             text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'porting', 'active', 'removed')),
  target_type        text NOT NULL DEFAULT 'none'
                     CHECK (target_type IN ('none', 'user', 'ring_group', 'queue', 'menu', 'ai')),
  target_id          uuid,
  site_id            uuid REFERENCES voice_sites(id) ON DELETE SET NULL,
  provider           text NOT NULL DEFAULT 'simulated',
  provider_ref       text NOT NULL DEFAULT '',
  emergency_address  jsonb NOT NULL DEFAULT '{}',
  order_ref          text UNIQUE,
  billing_from       timestamptz,
  billing_until      timestamptz,
  created_at         timestamptz NOT NULL DEFAULT now(),
  removed_at         timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS voice_numbers_live ON voice_numbers (e164) WHERE status <> 'removed';

-- Desk phones (set up from their MAC address) and softphones (sign-in link).
CREATE TABLE IF NOT EXISTS voice_devices (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id         uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  voice_user_id       uuid REFERENCES voice_users(id) ON DELETE CASCADE,
  kind                text NOT NULL CHECK (kind IN ('desk', 'softphone')),
  mac                 text,
  model               text NOT NULL DEFAULT '',
  token_hash          text NOT NULL DEFAULT '',
  token_expires_at    timestamptz,
  status              text NOT NULL DEFAULT 'waiting' CHECK (status IN ('waiting', 'provisioned', 'removed')),
  last_seen_at        timestamptz,
  order_ref           text UNIQUE,
  created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS voice_devices_mac ON voice_devices (mac) WHERE mac IS NOT NULL AND status <> 'removed';

-- Call routing configuration (versioned and rolled back as a whole).
CREATE TABLE IF NOT EXISTS voice_ring_groups (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id   uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  name          text NOT NULL,
  extension     text NOT NULL,
  strategy      text NOT NULL DEFAULT 'simultaneous' CHECK (strategy IN ('simultaneous', 'sequential')),
  members       uuid[] NOT NULL DEFAULT '{}',
  ring_seconds  int NOT NULL DEFAULT 20,
  UNIQUE (customer_id, name)
);
CREATE TABLE IF NOT EXISTS voice_queues (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id   uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  name          text NOT NULL,
  extension     text NOT NULL,
  strategy      text NOT NULL DEFAULT 'longest-idle-agent',
  members       uuid[] NOT NULL DEFAULT '{}',
  max_wait_s    int NOT NULL DEFAULT 300,
  UNIQUE (customer_id, name)
);
CREATE TABLE IF NOT EXISTS voice_hours (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  name         text NOT NULL,
  timezone     text NOT NULL DEFAULT 'America/Port_of_Spain',
  schedule     jsonb NOT NULL DEFAULT '{}',   -- {"mon": [["08:00", "17:00"]], ...}
  holidays     jsonb NOT NULL DEFAULT '[]',   -- ["2026-12-25", ...]
  UNIQUE (customer_id, name)
);
CREATE TABLE IF NOT EXISTS voice_menus (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  name         text NOT NULL,
  extension    text NOT NULL,
  greeting     text NOT NULL DEFAULT '',
  options      jsonb NOT NULL DEFAULT '{}',   -- {"1": {"type": "ring_group", "id": "..."}}
  hours_id     uuid,
  closed_target jsonb NOT NULL DEFAULT '{}',  -- where calls go outside hours
  UNIQUE (customer_id, name)
);
CREATE TABLE IF NOT EXISTS voice_ai_rules (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  name         text NOT NULL,
  condition    text NOT NULL CHECK (condition IN ('after_hours', 'no_answer', 'busy', 'always')),
  hours_id     uuid,
  number_id    uuid,                          -- NULL: every number
  fallback     text NOT NULL DEFAULT 'voicemail' CHECK (fallback IN ('voicemail', 'ring_group', 'queue')),
  enabled      boolean NOT NULL DEFAULT true,
  UNIQUE (customer_id, name)
);

-- Every configuration change makes a version with a full snapshot.
CREATE TABLE IF NOT EXISTS voice_config_versions (
  id           bigserial PRIMARY KEY,
  customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  version      int NOT NULL,
  kind         text NOT NULL DEFAULT 'change' CHECK (kind IN ('change', 'bulk', 'scheduled', 'rollback', 'order', 'chat')),
  summary      text NOT NULL DEFAULT '',
  ops          jsonb NOT NULL DEFAULT '[]',
  snapshot     jsonb NOT NULL,
  price_impact jsonb NOT NULL DEFAULT '{}',
  created_by   text NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (customer_id, version)
);

-- Changes set for a later time (run through the job queue).
CREATE TABLE IF NOT EXISTS voice_changes (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id   uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  summary       text NOT NULL DEFAULT '',
  ops           jsonb NOT NULL,
  run_at        timestamptz NOT NULL,
  status        text NOT NULL DEFAULT 'scheduled' CHECK (status IN ('scheduled', 'applied', 'failed', 'cancelled')),
  price_impact  jsonb NOT NULL DEFAULT '{}',
  created_by    text NOT NULL,
  version       int,
  error         text NOT NULL DEFAULT '',
  created_at    timestamptz NOT NULL DEFAULT now(),
  finished_at   timestamptz
);

-- Changes proposed from a sentence; applied only when the person confirms.
CREATE TABLE IF NOT EXISTS voice_chat_proposals (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  user_id      uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  text         text NOT NULL,
  ops          jsonb NOT NULL,
  summary      text NOT NULL,
  scope        text NOT NULL CHECK (scope IN ('self', 'admin')),
  status       text NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed', 'applied', 'cancelled')),
  created_at   timestamptz NOT NULL DEFAULT now(),
  expires_at   timestamptz NOT NULL DEFAULT now() + interval '15 minutes'
);

-- The rendered PBX configuration, per business (deterministic; see freeswitch.py).
CREATE TABLE IF NOT EXISTS voice_pbx_renders (
  customer_id  uuid PRIMARY KEY REFERENCES customers(id) ON DELETE CASCADE,
  digest       text NOT NULL,
  files        jsonb NOT NULL,
  written_to   text NOT NULL DEFAULT '',
  rendered_at  timestamptz NOT NULL DEFAULT now()
);

-- ---- provisioning ------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS voice_orders (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id   uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  status        text NOT NULL DEFAULT 'draft'
                CHECK (status IN ('draft', 'approved', 'provisioning', 'failed', 'active', 'cancelled')),
  items         jsonb NOT NULL,
  price         jsonb NOT NULL DEFAULT '{}',
  rate_card_id  uuid,
  failed_step   text NOT NULL DEFAULT '',
  error         text NOT NULL DEFAULT '',
  retries       int NOT NULL DEFAULT 0,
  created_by    text NOT NULL,
  approved_by   text NOT NULL DEFAULT '',
  approved_at   timestamptz,
  activated_at  timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS voice_order_steps (
  order_id     uuid NOT NULL REFERENCES voice_orders(id) ON DELETE CASCADE,
  position     int NOT NULL,
  step         text NOT NULL CHECK (step IN ('tenant', 'numbers', 'devices', 'confirm', 'billing')),
  status       text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done', 'failed')),
  attempts     int NOT NULL DEFAULT 0,
  error        text NOT NULL DEFAULT '',
  result       jsonb NOT NULL DEFAULT '{}',
  updated_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (order_id, step)
);
CREATE TABLE IF NOT EXISTS voice_port_orders (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id     uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  order_id        uuid REFERENCES voice_orders(id) ON DELETE SET NULL,
  number_id       uuid REFERENCES voice_numbers(id) ON DELETE SET NULL,
  e164            text NOT NULL,
  losing_carrier  text NOT NULL DEFAULT '',
  status          text NOT NULL DEFAULT 'submitted'
                  CHECK (status IN ('submitted', 'accepted', 'scheduled', 'completed', 'rejected', 'cancelled')),
  switch_date     date,
  provider_ref    text NOT NULL DEFAULT '',
  note            text NOT NULL DEFAULT '',
  order_ref       text UNIQUE,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS voice_test_calls (
  id          bigserial PRIMARY KEY,
  customer_id uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  order_id    uuid,
  number_id   uuid,
  e164        text NOT NULL,
  ok          boolean NOT NULL,
  detail      text NOT NULL DEFAULT '',
  at          timestamptz NOT NULL DEFAULT now()
);

-- The simulated SIP provider's own records (a stand-in for the provider's side).
-- The idempotency key means a retried order never gets a second number.
CREATE TABLE IF NOT EXISTS voice_sim_provider_numbers (
  e164             text PRIMARY KEY,
  customer_id      uuid NOT NULL,
  idempotency_key  text NOT NULL UNIQUE,
  released         boolean NOT NULL DEFAULT false,
  created_at       timestamptz NOT NULL DEFAULT now()
);

-- ---- billing -----------------------------------------------------------------------

-- Rate cards per business, versioned. A new version never changes an old one.
CREATE TABLE IF NOT EXISTS voice_rate_cards (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id     uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  version         int NOT NULL,
  label           text NOT NULL DEFAULT '',
  example         boolean NOT NULL DEFAULT false,
  currency        text NOT NULL DEFAULT 'USD',
  effective_from  timestamptz NOT NULL DEFAULT now(),
  monthly_user    numeric(12, 4) NOT NULL,
  monthly_number  numeric(12, 4) NOT NULL,
  ai_minute       numeric(12, 4) NOT NULL,
  one_time        jsonb NOT NULL DEFAULT '{}',   -- {"desk_phone": "25.00", ...} as strings
  destinations    jsonb NOT NULL DEFAULT '[]',   -- [{"prefix": "1868", "name": "...", "per_minute": "0.015"}]
  created_by      text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (customer_id, version)
);

-- Call records. Rated once, when the call ends.
CREATE TABLE IF NOT EXISTS voice_cdrs (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id     uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  call_id         text NOT NULL,
  direction       text NOT NULL CHECK (direction IN ('outbound', 'inbound', 'internal')),
  from_number     text NOT NULL DEFAULT '',
  to_number       text NOT NULL DEFAULT '',
  voice_user_id   uuid REFERENCES voice_users(id) ON DELETE SET NULL,
  site_id         uuid,
  team_id         uuid,
  started_at      timestamptz NOT NULL,
  ended_at        timestamptz NOT NULL,
  seconds         int NOT NULL DEFAULT 0,
  ai_seconds      int NOT NULL DEFAULT 0,
  status          text NOT NULL DEFAULT 'completed' CHECK (status IN ('completed', 'blocked', 'failed', 'missed')),
  block_reason    text NOT NULL DEFAULT '',
  recording_ref   text NOT NULL DEFAULT '',
  provider_ref    text NOT NULL DEFAULT '',
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (customer_id, call_id)
);
CREATE INDEX IF NOT EXISTS voice_cdrs_customer ON voice_cdrs (customer_id, ended_at DESC);
CREATE INDEX IF NOT EXISTS voice_cdrs_provider ON voice_cdrs (provider_ref) WHERE provider_ref <> '';

-- Included minutes, pooled across the business, reset each calendar month.
CREATE TABLE IF NOT EXISTS voice_bundles (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  name         text NOT NULL,
  minutes      numeric(12, 2) NOT NULL,
  prefixes     text[] NOT NULL DEFAULT '{}',    -- destinations it covers; empty means all outbound
  alert_pct    int NOT NULL DEFAULT 80,
  active       boolean NOT NULL DEFAULT true,
  created_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (customer_id, name)
);
CREATE TABLE IF NOT EXISTS voice_bundle_alerts (
  bundle_id  uuid NOT NULL REFERENCES voice_bundles(id) ON DELETE CASCADE,
  period     date NOT NULL,
  level      int NOT NULL,
  at         timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (bundle_id, period, level)
);

-- Fraud limits that stop calls.
CREATE TABLE IF NOT EXISTS voice_fraud_limits (
  customer_id         uuid PRIMARY KEY REFERENCES customers(id) ON DELETE CASCADE,
  daily_cap           numeric(12, 2),
  blocked_prefixes    text[] NOT NULL DEFAULT '{1900,1976,881,882,883,979}',
  international       boolean NOT NULL DEFAULT true,
  calls_per_hour_alert int NOT NULL DEFAULT 60,
  updated_by          text NOT NULL DEFAULT '',
  updated_at          timestamptz NOT NULL DEFAULT now()
);

-- Invoices: draft, then issued and frozen. Corrections are credit notes.
CREATE TABLE IF NOT EXISTS voice_invoices (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id   uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  kind          text NOT NULL DEFAULT 'invoice' CHECK (kind IN ('invoice', 'credit_note')),
  number        text,
  status        text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'issued')),
  period_start  date NOT NULL,
  period_end    date NOT NULL,
  currency      text NOT NULL DEFAULT 'USD',
  total         numeric(14, 2) NOT NULL DEFAULT 0,
  credits_id    uuid REFERENCES voice_invoices(id),
  reason        text NOT NULL DEFAULT '',
  created_by    text NOT NULL,
  issued_by     text NOT NULL DEFAULT '',
  issued_at     timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (customer_id, number)
);
CREATE UNIQUE INDEX IF NOT EXISTS voice_invoices_one_draft
  ON voice_invoices (customer_id, period_start) WHERE status = 'draft' AND kind = 'invoice';

-- Rated charges: calls, AI minutes, monthly fees, one-time fees.
CREATE TABLE IF NOT EXISTS voice_charges (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id        uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  ref                text NOT NULL,
  kind               text NOT NULL CHECK (kind IN ('call', 'ai_minutes', 'monthly_user', 'monthly_number', 'one_time')),
  description        text NOT NULL,
  cdr_id             uuid REFERENCES voice_cdrs(id) ON DELETE SET NULL,
  rate_card_id       uuid REFERENCES voice_rate_cards(id),
  rate_card_version  int,
  destination        text NOT NULL DEFAULT '',
  quantity           numeric(14, 4) NOT NULL DEFAULT 0,
  bundle_id          uuid REFERENCES voice_bundles(id) ON DELETE SET NULL,
  bundle_minutes     numeric(14, 4) NOT NULL DEFAULT 0,
  unit_price         numeric(12, 4) NOT NULL DEFAULT 0,
  amount             numeric(14, 4) NOT NULL DEFAULT 0,
  site_id            uuid,
  team_id            uuid,
  voice_user_id      uuid,
  at                 timestamptz NOT NULL DEFAULT now(),
  invoice_id         uuid REFERENCES voice_invoices(id),
  UNIQUE (customer_id, ref)
);
CREATE INDEX IF NOT EXISTS voice_charges_period ON voice_charges (customer_id, at);

CREATE TABLE IF NOT EXISTS voice_invoice_lines (
  id           bigserial PRIMARY KEY,
  invoice_id   uuid NOT NULL REFERENCES voice_invoices(id) ON DELETE CASCADE,
  customer_id  uuid NOT NULL,
  charge_id    uuid REFERENCES voice_charges(id),
  credits_line bigint REFERENCES voice_invoice_lines(id),
  description  text NOT NULL,
  quantity     numeric(14, 4) NOT NULL DEFAULT 0,
  amount       numeric(14, 4) NOT NULL
);

-- Issued invoices and their lines can never change or be removed.
CREATE OR REPLACE FUNCTION voice_invoice_frozen() RETURNS trigger AS $$
BEGIN
  -- Removing the whole business (cascade) is the one exception.
  IF TG_OP = 'DELETE' AND NOT EXISTS (SELECT 1 FROM customers WHERE id = OLD.customer_id) THEN
    RETURN OLD;
  END IF;
  IF TG_TABLE_NAME = 'voice_invoices' THEN
    IF OLD.status = 'issued' THEN
      RAISE EXCEPTION 'voice invoice % is issued and frozen', OLD.number USING ERRCODE = 'check_violation';
    END IF;
  ELSE
    IF EXISTS (SELECT 1 FROM voice_invoices WHERE id = OLD.invoice_id AND status = 'issued') THEN
      RAISE EXCEPTION 'lines of an issued voice invoice are frozen' USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS voice_invoices_frozen ON voice_invoices;
CREATE TRIGGER voice_invoices_frozen BEFORE UPDATE OR DELETE ON voice_invoices
  FOR EACH ROW EXECUTE FUNCTION voice_invoice_frozen();
DROP TRIGGER IF EXISTS voice_invoice_lines_frozen ON voice_invoice_lines;
CREATE TRIGGER voice_invoice_lines_frozen BEFORE UPDATE OR DELETE ON voice_invoice_lines
  FOR EACH ROW EXECUTE FUNCTION voice_invoice_frozen();

-- Supplier side: what the SIP provider charges ExaCarib, imported from its CSV.
CREATE TABLE IF NOT EXISTS voice_supplier_imports (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  supplier     text NOT NULL,
  filename     text NOT NULL DEFAULT '',
  rows         int NOT NULL DEFAULT 0,
  rejected     jsonb NOT NULL DEFAULT '[]',
  created_by   text NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS voice_supplier_charges (
  id            bigserial PRIMARY KEY,
  import_id     uuid NOT NULL REFERENCES voice_supplier_imports(id) ON DELETE CASCADE,
  supplier      text NOT NULL,
  call_ref      text NOT NULL,
  started_at    timestamptz,
  destination   text NOT NULL DEFAULT '',
  seconds       int NOT NULL DEFAULT 0,
  cost          numeric(14, 4) NOT NULL,
  customer_id   uuid,
  cdr_id        uuid,
  UNIQUE (supplier, call_ref)
);
