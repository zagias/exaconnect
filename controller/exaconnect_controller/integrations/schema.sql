-- Connect integrations (ADR 0026). Applied after schema.sql and the CommAI files,
-- so the durable job queue (jobs) already exists.

-- Every event Connect publishes, as a CloudEvent. The id is stable, so a
-- receiver de-duplicates on it.
CREATE TABLE IF NOT EXISTS connect_events (
  id           text PRIMARY KEY,
  type         text NOT NULL,
  source       text NOT NULL,
  subject      text NOT NULL DEFAULT '',
  time         timestamptz NOT NULL,
  customer_id  uuid,
  carrier_ids  uuid[] NOT NULL DEFAULT '{}',
  site_id      uuid,
  severity     text NOT NULL DEFAULT 'info' CHECK (severity IN ('info', 'warning', 'critical')),
  dedup_key    text NOT NULL DEFAULT '',
  action       text NOT NULL DEFAULT 'trigger' CHECK (action IN ('trigger', 'resolve', 'notify')),
  data         jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS connect_events_customer_time ON connect_events (customer_id, time DESC);

-- One row per destination: a webhook, an alerting or ITSM tool, a SIEM, a
-- metrics backend, an inventory or a REST hook made by Zapier, Make or n8n.
-- Owned by a customer or by a carrier (events on its own links only).
CREATE TABLE IF NOT EXISTS connect_integrations (
  id                 bigserial PRIMARY KEY,
  customer_id        uuid REFERENCES customers(id) ON DELETE CASCADE,
  carrier_id         uuid REFERENCES carriers(id) ON DELETE CASCADE,
  provider           text NOT NULL,
  name               text NOT NULL,
  enabled            boolean NOT NULL DEFAULT true,
  config             jsonb NOT NULL DEFAULT '{}',
  -- Fernet ciphertext of the secret fields (EXA_SECRETS_KEY); never returned.
  secret_ciphertext  text NOT NULL DEFAULT '',
  secret_fields      text[] NOT NULL DEFAULT '{}',
  event_types        text[] NOT NULL DEFAULT '{*}',
  site_ids           uuid[] NOT NULL DEFAULT '{}',
  min_severity       text NOT NULL DEFAULT 'info' CHECK (min_severity IN ('info', 'warning', 'critical')),
  origin             text NOT NULL DEFAULT 'portal' CHECK (origin IN ('portal', 'api', 'resthook', 'tmf688')),
  state              jsonb NOT NULL DEFAULT '{}',
  last_status        text NOT NULL DEFAULT '',
  last_delivery_at   timestamptz,
  created_by         text NOT NULL,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),
  CHECK ((customer_id IS NULL) <> (carrier_id IS NULL))
);
CREATE INDEX IF NOT EXISTS connect_integrations_customer ON connect_integrations (customer_id) WHERE enabled;
CREATE INDEX IF NOT EXISTS connect_integrations_carrier ON connect_integrations (carrier_id) WHERE enabled;

CREATE TABLE IF NOT EXISTS connect_deliveries (
  id              bigserial PRIMARY KEY,
  integration_id  bigint NOT NULL REFERENCES connect_integrations(id) ON DELETE CASCADE,
  event_id        text NOT NULL,
  event_type      text NOT NULL,
  status          text NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'delivered', 'simulated', 'failed', 'digest', 'skipped')),
  attempts        int NOT NULL DEFAULT 0,
  response_code   int,
  last_error      text NOT NULL DEFAULT '',
  detail          jsonb NOT NULL DEFAULT '{}',
  test            boolean NOT NULL DEFAULT false,
  created_at      timestamptz NOT NULL DEFAULT now(),
  delivered_at    timestamptz,
  UNIQUE (integration_id, event_id)
);
CREATE INDEX IF NOT EXISTS connect_deliveries_integration ON connect_deliveries (integration_id, id DESC);

-- What an alerting or ITSM tool calls the incident a dedup key opened, so a
-- recovery resolves it (ServiceNow sys_id, Jira issue key, Opsgenie alias).
CREATE TABLE IF NOT EXISTS connect_incidents (
  integration_id  bigint NOT NULL REFERENCES connect_integrations(id) ON DELETE CASCADE,
  dedup_key       text NOT NULL,
  external_id     text NOT NULL,
  state           text NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'resolved')),
  opened_at       timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (integration_id, dedup_key)
);

-- Requests a simulated provider would have sent. Secrets are never stored.
CREATE TABLE IF NOT EXISTS connect_sim_outbox (
  id              bigserial PRIMARY KEY,
  integration_id  bigint,
  provider        text NOT NULL,
  method          text NOT NULL,
  target          text NOT NULL,
  body            text NOT NULL DEFAULT '',
  at              timestamptz NOT NULL DEFAULT now()
);

-- Carrier notices (planned maintenance, faults) and trouble tickets, from
-- the portal, the API, TMF621, TMF688, MEF LSO Sonata or an email gateway.
CREATE TABLE IF NOT EXISTS connect_notices (
  id             bigserial PRIMARY KEY,
  carrier_id     uuid REFERENCES carriers(id) ON DELETE CASCADE,
  customer_id    uuid REFERENCES customers(id) ON DELETE CASCADE,
  kind           text NOT NULL CHECK (kind IN ('maintenance', 'fault', 'trouble')),
  status         text NOT NULL DEFAULT 'open'
                 CHECK (status IN ('scheduled', 'open', 'in_progress', 'resolved', 'cancelled', 'closed')),
  severity       text NOT NULL DEFAULT 'warning' CHECK (severity IN ('info', 'warning', 'critical')),
  title          text NOT NULL,
  description    text NOT NULL DEFAULT '',
  link_ids       uuid[] NOT NULL DEFAULT '{}',
  starts_at      timestamptz,
  ends_at        timestamptz,
  move_traffic   boolean NOT NULL DEFAULT true,
  external_id    text NOT NULL DEFAULT '',
  source         text NOT NULL DEFAULT 'api'
                 CHECK (source IN ('portal', 'api', 'email', 'tmf621', 'tmf688', 'sonata', 'connect')),
  raw            jsonb NOT NULL DEFAULT '{}',
  window_state   text NOT NULL DEFAULT '' CHECK (window_state IN ('', 'started', 'ended')),
  created_by     text NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now(),
  resolved_at    timestamptz
);
CREATE INDEX IF NOT EXISTS connect_notices_carrier ON connect_notices (carrier_id, id DESC);
CREATE INDEX IF NOT EXISTS connect_notices_links ON connect_notices USING gin (link_ids);
CREATE UNIQUE INDEX IF NOT EXISTS connect_notices_external
  ON connect_notices (carrier_id, external_id) WHERE external_id <> '';

-- Cloud on-ramps and partner connectivity ordered through a provider adapter.
CREATE TABLE IF NOT EXISTS connect_onramps (
  id              bigserial PRIMARY KEY,
  customer_id     uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  provider        text NOT NULL,
  name            text NOT NULL,
  site_id         uuid REFERENCES sites(id) ON DELETE SET NULL,
  location        text NOT NULL DEFAULT '',
  bandwidth_mbps  int NOT NULL CHECK (bandwidth_mbps BETWEEN 1 AND 100000),
  external_id     text NOT NULL DEFAULT '',
  pairing         jsonb NOT NULL DEFAULT '{}',
  provider_state  text NOT NULL DEFAULT '',
  status          text NOT NULL DEFAULT 'ordering'
                  CHECK (status IN ('ordering', 'pending', 'available', 'failed', 'deleting', 'deleted')),
  simulated       boolean NOT NULL DEFAULT true,
  order_id        bigint,
  detail          jsonb NOT NULL DEFAULT '{}',
  created_by      text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS connect_onramps_customer ON connect_onramps (customer_id, id DESC);

-- Documents behind the standards APIs that have no table of their own:
-- MEF LSO Sonata qualifications and quotes, TMF622 and Sonata product orders.
CREATE TABLE IF NOT EXISTS connect_std_documents (
  id           text PRIMARY KEY,
  kind         text NOT NULL,
  customer_id  uuid REFERENCES customers(id) ON DELETE CASCADE,
  state        text NOT NULL,
  body         jsonb NOT NULL,
  order_id     bigint,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS connect_std_documents_customer ON connect_std_documents (customer_id, kind);

ALTER TABLE nodes ADD COLUMN IF NOT EXISTS offline_since timestamptz;

-- Publishing without slowing the hot paths: a row trigger queues one job in
-- the writer's own transaction, and only when somebody is listening.
CREATE OR REPLACE FUNCTION connect_publish() RETURNS trigger AS $$
BEGIN
  IF TG_ARGV[0] = 'events' THEN
    IF NEW.kind NOT IN ('bfd_down', 'bfd_up', 'storm_on', 'storm_off', 'insight', 'enrolled', 'node_revoked',
                        'node_offline', 'node_online', 'config_failed', 'config_rolled_back', 'ddos_blocked',
                        'carrier_notice', 'maintenance_start', 'maintenance_end', 'sla_breach',
                        'sla_breach_forecast') THEN
      RETURN NULL;
    END IF;
  ELSIF NEW.kind = 'hold' THEN
    RETURN NULL;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM connect_integrations i
                 WHERE i.enabled AND (i.customer_id = NEW.customer_id OR i.carrier_id IS NOT NULL)) THEN
    RETURN NULL;
  END IF;
  INSERT INTO jobs (kind, customer_id, payload)
  VALUES ('integrations.publish', NEW.customer_id,
          jsonb_build_object('source', TG_ARGV[0], 'row', to_jsonb(NEW)));
  RETURN NULL;
END
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS connect_publish_events ON events;
CREATE TRIGGER connect_publish_events AFTER INSERT ON events
  FOR EACH ROW EXECUTE FUNCTION connect_publish('events');
DROP TRIGGER IF EXISTS connect_publish_decisions ON decisions;
CREATE TRIGGER connect_publish_decisions AFTER INSERT ON decisions
  FOR EACH ROW EXECUTE FUNCTION connect_publish('decisions');
