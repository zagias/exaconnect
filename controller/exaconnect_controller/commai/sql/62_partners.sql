-- Partners, white-label, regional hosting and the developer platform (ADR 0025).
-- Idempotent; applied after 55_golive.sql.

-- ---- Partners ------------------------------------------------------------------------
-- A reseller or managed-service provider that looks after several businesses.
-- ("partners" is Connect's carrier-partner catalogue, so these are commai_partners.)
CREATE TABLE IF NOT EXISTS commai_partners (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name           text NOT NULL UNIQUE,
  kind           text NOT NULL CHECK (kind IN ('reseller', 'msp')),
  markup_pct     numeric(6, 2) NOT NULL DEFAULT 0 CHECK (markup_pct >= 0 AND markup_pct <= 500),
  contact_email  text NOT NULL DEFAULT '',
  active         boolean NOT NULL DEFAULT true,
  created_by     text NOT NULL DEFAULT '',
  created_at     timestamptz NOT NULL DEFAULT now()
);

-- The partner's own people. A person belongs to one partner at most.
CREATE TABLE IF NOT EXISTS commai_partner_members (
  partner_id  uuid NOT NULL REFERENCES commai_partners(id) ON DELETE CASCADE,
  user_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role        text NOT NULL DEFAULT 'member' CHECK (role IN ('admin', 'member')),
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (partner_id, user_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS commai_partner_members_user ON commai_partner_members (user_id);

-- A partner asks to manage a business; the business accepts (choosing what the
-- partner may do), and can revoke at any time.
CREATE TABLE IF NOT EXISTS commai_partner_links (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  partner_id        uuid NOT NULL REFERENCES commai_partners(id) ON DELETE CASCADE,
  customer_id       uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  status            text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'active', 'declined', 'revoked')),
  requested_scopes  text[] NOT NULL,
  scopes            text[] NOT NULL DEFAULT '{}',
  markup_pct        numeric(6, 2) CHECK (markup_pct IS NULL OR (markup_pct >= 0 AND markup_pct <= 500)),
  white_label       boolean NOT NULL DEFAULT true,
  note              text NOT NULL DEFAULT '',
  requested_by      text NOT NULL DEFAULT '',
  requested_at      timestamptz NOT NULL DEFAULT now(),
  decided_by        text NOT NULL DEFAULT '',
  decided_at        timestamptz,
  revoked_by        text NOT NULL DEFAULT '',
  revoked_at        timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS commai_partner_links_open
  ON commai_partner_links (partner_id, customer_id) WHERE status IN ('pending', 'active');
-- One managing partner per business at a time.
CREATE UNIQUE INDEX IF NOT EXISTS commai_partner_links_one_active
  ON commai_partner_links (customer_id) WHERE status = 'active';

-- When a partner person switches into a business they act through a delegate
-- account of that business, limited to the scopes the business granted.
CREATE TABLE IF NOT EXISTS commai_partner_delegates (
  link_id           uuid NOT NULL REFERENCES commai_partner_links(id) ON DELETE CASCADE,
  partner_user_id   uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  delegate_user_id  uuid NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
  created_at        timestamptz NOT NULL DEFAULT now(),
  last_switch_at    timestamptz,
  PRIMARY KEY (link_id, partner_user_id)
);

-- Monthly statements: usage at example prices plus the partner's markup.
-- Recorded for billing; no payments are taken here.
CREATE TABLE IF NOT EXISTS commai_partner_statements (
  partner_id    uuid NOT NULL REFERENCES commai_partners(id) ON DELETE CASCADE,
  customer_id   uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  period        date NOT NULL,                     -- first day of the month
  base_amount   numeric(14, 4) NOT NULL,
  markup_pct    numeric(6, 2) NOT NULL,
  total_amount  numeric(14, 4) NOT NULL,
  currency      text NOT NULL DEFAULT 'USD',
  lines         jsonb NOT NULL DEFAULT '[]',
  recorded_by   text NOT NULL DEFAULT '',
  recorded_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (partner_id, customer_id, period)
);

-- ---- White-label ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS commai_brands (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  partner_id     uuid UNIQUE REFERENCES commai_partners(id) ON DELETE CASCADE,
  customer_id    uuid UNIQUE REFERENCES customers(id) ON DELETE CASCADE,
  product_name   text NOT NULL,
  colour         text NOT NULL,                    -- buttons and links (#RRGGBB)
  ink            text NOT NULL DEFAULT '#10213D',  -- text
  support_email  text NOT NULL DEFAULT '',
  logo           bytea,
  logo_type      text NOT NULL DEFAULT '',
  logo_sha       text NOT NULL DEFAULT '',
  updated_by     text NOT NULL DEFAULT '',
  updated_at     timestamptz NOT NULL DEFAULT now(),
  CHECK ((partner_id IS NULL) <> (customer_id IS NULL))
);

-- Custom domains for the portal and the chat widget. A certificate is issued
-- (by Caddy, on demand) only once the DNS TXT record proves the domain.
CREATE TABLE IF NOT EXISTS commai_brand_domains (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  brand_id         uuid NOT NULL REFERENCES commai_brands(id) ON DELETE CASCADE,
  purpose          text NOT NULL CHECK (purpose IN ('portal', 'widget')),
  domain           text NOT NULL UNIQUE,
  token            text NOT NULL,
  status           text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'verified', 'failed')),
  last_error       text NOT NULL DEFAULT '',
  last_checked_at  timestamptz,
  verified_at      timestamptz,
  created_by       text NOT NULL DEFAULT '',
  created_at       timestamptz NOT NULL DEFAULT now()
);

-- The simulated DNS resolver's records (stand-in for the public DNS).
CREATE TABLE IF NOT EXISTS commai_sim_dns (
  name   text NOT NULL,
  value  text NOT NULL,
  PRIMARY KEY (name, value)
);

-- ---- Regional hosting ------------------------------------------------------------------
-- For each declared region (kind 'region' in the go-live registry), the provider
-- of each dependency in that region.
CREATE TABLE IF NOT EXISTS commai_region_providers (
  region           text NOT NULL,
  dependency       text NOT NULL,
  provider         text NOT NULL,
  provider_region  text NOT NULL,
  notes            text NOT NULL DEFAULT '',
  recorded_by      text NOT NULL DEFAULT '',
  recorded_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (region, dependency)
);
ALTER TABLE customers ADD COLUMN IF NOT EXISTS home_region text NOT NULL DEFAULT 'primary';

-- A region's dependency criterion cannot be marked met (through any API)
-- unless a provider in that region is recorded for it. The update goes
-- through but stays unmet, so switching the region on is refused with the
-- usual "not every criterion is met".
CREATE OR REPLACE FUNCTION commai_region_dependency_guard() RETURNS trigger AS $$
BEGIN
  IF NEW.kind = 'region' AND NEW.criterion LIKE 'dependency-%' AND NEW.met THEN
    IF NOT EXISTS (
      SELECT 1 FROM commai_region_providers p
      WHERE p.region = NEW.key AND p.dependency = substr(NEW.criterion, 12)
        AND p.provider <> '' AND p.provider_region <> ''
    ) THEN
      NEW.met := false;
      NEW.evidence := 'Refused: no provider in this region is recorded for this dependency.';
    END IF;
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS commai_region_dependency_guard ON commai_capability_criteria;
CREATE TRIGGER commai_region_dependency_guard BEFORE INSERT OR UPDATE ON commai_capability_criteria
  FOR EACH ROW EXECUTE FUNCTION commai_region_dependency_guard();

-- ---- Developer platform: OAuth 2.0 for partner apps ----------------------------------
CREATE TABLE IF NOT EXISTS commai_oauth_clients (
  client_id      text PRIMARY KEY,
  partner_id     uuid NOT NULL REFERENCES commai_partners(id) ON DELETE CASCADE,
  name           text NOT NULL,
  redirect_uris  text[] NOT NULL,
  scopes         text[] NOT NULL,
  confidential   boolean NOT NULL DEFAULT false,
  secret_hash    text NOT NULL DEFAULT '',
  created_by     text NOT NULL DEFAULT '',
  created_at     timestamptz NOT NULL DEFAULT now(),
  revoked_at     timestamptz
);

-- Authorisation codes: single use, ten minutes, bound to a PKCE challenge.
CREATE TABLE IF NOT EXISTS commai_oauth_codes (
  code_hash       text PRIMARY KEY,
  client_id       text NOT NULL REFERENCES commai_oauth_clients(client_id) ON DELETE CASCADE,
  user_id         uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  customer_id     uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  scopes          text[] NOT NULL,
  redirect_uri    text NOT NULL,
  code_challenge  text NOT NULL,
  expires_at      timestamptz NOT NULL,
  used_at         timestamptz,
  grant_id        uuid
);

-- A consent that produced tokens. The access token is an expiring API key
-- (api_keys row); the refresh token rotates on every use.
CREATE TABLE IF NOT EXISTS commai_oauth_grants (
  id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id              text NOT NULL REFERENCES commai_oauth_clients(client_id) ON DELETE CASCADE,
  user_id                uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  customer_id            uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  scopes                 text[] NOT NULL,
  refresh_hash           text NOT NULL UNIQUE,
  previous_refresh_hash  text,
  access_key_id          bigint REFERENCES api_keys(id) ON DELETE SET NULL,
  created_at             timestamptz NOT NULL DEFAULT now(),
  refreshed_at           timestamptz,
  expires_at             timestamptz NOT NULL,
  revoked_at             timestamptz,
  revoked_by             text NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS commai_oauth_grants_customer ON commai_oauth_grants (customer_id) WHERE revoked_at IS NULL;

-- ---- Developer platform: sandboxes ------------------------------------------------------
-- A sandbox is a copy of a business (its own customers row) where every
-- channel uses the simulated provider. Sandbox keys act only there.
ALTER TABLE customers ADD COLUMN IF NOT EXISTS sandbox_of uuid REFERENCES customers(id) ON DELETE CASCADE;
CREATE UNIQUE INDEX IF NOT EXISTS customers_sandbox_of ON customers (sandbox_of) WHERE sandbox_of IS NOT NULL;
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS sandbox boolean NOT NULL DEFAULT false;

-- Whatever the API asks, a sandbox business's channel accounts use the
-- simulated provider: no real WhatsApp, SMS or email can leave a sandbox.
CREATE OR REPLACE FUNCTION commai_sandbox_simulated_only() RETURNS trigger AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM customers WHERE id = NEW.customer_id AND sandbox_of IS NOT NULL) THEN
    NEW.provider := 'simulated';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS commai_sandbox_simulated_only ON channel_accounts;
CREATE TRIGGER commai_sandbox_simulated_only BEFORE INSERT OR UPDATE ON channel_accounts
  FOR EACH ROW EXECUTE FUNCTION commai_sandbox_simulated_only();

-- Real numbers and porting are never ordered for a sandbox.
CREATE OR REPLACE FUNCTION commai_sandbox_no_orders() RETURNS trigger AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM customers WHERE id = NEW.customer_id AND sandbox_of IS NOT NULL) THEN
    RAISE EXCEPTION 'A sandbox cannot order numbers or porting.' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS commai_sandbox_no_orders ON voice_orders;
CREATE TRIGGER commai_sandbox_no_orders BEFORE INSERT ON voice_orders
  FOR EACH ROW EXECUTE FUNCTION commai_sandbox_no_orders();
DROP TRIGGER IF EXISTS commai_sandbox_no_ports ON voice_port_orders;
CREATE TRIGGER commai_sandbox_no_ports BEFORE INSERT ON voice_port_orders
  FOR EACH ROW EXECUTE FUNCTION commai_sandbox_no_orders();
