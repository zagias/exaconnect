-- CommAI phase 3 (ADR 0023): Messenger, Instagram and Telegram channels, the
-- country capability matrix's SMS rules, and SMS routing across carriers.
-- Applied after 55_golive.sql, idempotently, at startup.

-- Channel accounts now carry more channels than phase 2's three. The channel
-- name is checked by the code (channels.register); the table only keeps it tidy.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'channel_accounts_channel_check'
      AND pg_get_constraintdef(oid) LIKE '%whatsapp%'
  ) THEN
    ALTER TABLE channel_accounts DROP CONSTRAINT channel_accounts_channel_check;
    ALTER TABLE channel_accounts ADD CONSTRAINT channel_accounts_channel_check
      CHECK (channel ~ '^[a-z][a-z0-9_]{1,30}$');
  END IF;
END $$;

-- A real Facebook Page or Instagram account can be connected to one business
-- only: Meta sends one app-wide webhook and we route it by the Page or account id.
CREATE UNIQUE INDEX IF NOT EXISTS channel_accounts_meta_page
  ON channel_accounts (channel, address) WHERE provider = 'meta';

-- ExaCarib's SMS route table: per destination country, a primary and a
-- fallback carrier, with what each costs per message segment.
CREATE TABLE IF NOT EXISTS sms_routes (
  country           text PRIMARY KEY,
  primary_carrier   text NOT NULL,
  fallback_carrier  text,
  primary_cost      numeric(10, 5) NOT NULL DEFAULT 0,
  fallback_cost     numeric(10, 5) NOT NULL DEFAULT 0,
  currency          text NOT NULL DEFAULT 'USD',
  updated_by        text NOT NULL DEFAULT '',
  updated_at        timestamptz NOT NULL DEFAULT now(),
  CHECK (fallback_carrier IS NULL OR fallback_carrier <> primary_carrier)
);

-- Every attempt to hand one message to a carrier. 'unknown' means the carrier
-- may have taken it (a timeout): the gateway then retries that carrier only,
-- with the same idempotency key, and never fails over.
CREATE TABLE IF NOT EXISTS sms_route_attempts (
  id            bigserial PRIMARY KEY,
  customer_id   uuid NOT NULL,
  message_id    uuid NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  country       text NOT NULL,
  carrier       text NOT NULL,
  outcome       text NOT NULL CHECK (outcome IN ('accepted', 'refused', 'unknown')),
  provider_ref  text NOT NULL DEFAULT '',
  error         text NOT NULL DEFAULT '',
  cost          numeric(10, 5) NOT NULL DEFAULT 0,
  at            timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sms_route_attempts_message ON sms_route_attempts (message_id, id);
CREATE INDEX IF NOT EXISTS sms_route_attempts_customer ON sms_route_attempts (customer_id, id DESC);

-- Faults injected into the simulated carriers (tests and demonstrations).
CREATE TABLE IF NOT EXISTS sms_sim_faults (
  carrier     text PRIMARY KEY,
  mode        text NOT NULL CHECK (mode IN ('refuse', 'timeout', 'timeout_after_send')),
  updated_by  text NOT NULL DEFAULT '',
  updated_at  timestamptz NOT NULL DEFAULT now()
);

-- ExaCarib's adjustments to a country's SMS rules (rate, quiet hours, sender
-- types), on top of the researched matrix in countries.py.
CREATE TABLE IF NOT EXISTS country_sms_overrides (
  country     text PRIMARY KEY,
  rules       jsonb NOT NULL DEFAULT '{}',
  updated_by  text NOT NULL DEFAULT '',
  updated_at  timestamptz NOT NULL DEFAULT now()
);

-- Sender registrations a country requires (US 10DLC brand and campaign,
-- toll-free verification). The business asks; ExaCarib records the outcome.
CREATE TABLE IF NOT EXISTS sms_sender_registrations (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  sender       text NOT NULL,
  country      text NOT NULL,
  kind         text NOT NULL,
  status       text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
  reference    text NOT NULL DEFAULT '',
  note         text NOT NULL DEFAULT '',
  created_by   text NOT NULL DEFAULT '',
  decided_by   text NOT NULL DEFAULT '',
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (customer_id, sender, country)
);
