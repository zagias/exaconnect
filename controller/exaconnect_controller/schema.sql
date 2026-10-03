-- ExaConnect controller schema. Applied idempotently at startup.
-- Every customer-owned record carries customer_id (CLAUDE.md §1).

CREATE TABLE IF NOT EXISTS customers (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name        text NOT NULL UNIQUE,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS carriers (
  id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name  text NOT NULL UNIQUE
);

-- Reference data: the three path types every site can have.
CREATE TABLE IF NOT EXISTS paths (
  name          text PRIMARY KEY,             -- carrier-a, carrier-b, sat
  label         text NOT NULL,                -- Carrier A, Carrier B, Satellite
  tunnel        text NOT NULL UNIQUE,         -- wg-a, wg-b, wg-sat
  overlay_cidr  cidr NOT NULL,
  pop_port      int  NOT NULL,
  bfd_profile   text NOT NULL,
  local_pref    int  NOT NULL,
  ordinal       int  NOT NULL
);

CREATE TABLE IF NOT EXISTS sites (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id   uuid NOT NULL REFERENCES customers(id),
  name          text NOT NULL,
  kind          text NOT NULL CHECK (kind IN ('site', 'pop')),
  location      text NOT NULL DEFAULT '',
  timezone      text NOT NULL DEFAULT 'UTC',
  asn           int  NOT NULL,
  lan_prefixes  cidr[] NOT NULL DEFAULT '{}',
  overlay_host  int  NOT NULL CHECK (overlay_host BETWEEN 1 AND 254),
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (customer_id, name),
  UNIQUE (customer_id, overlay_host)
);

CREATE TABLE IF NOT EXISTS links (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id         uuid NOT NULL REFERENCES customers(id),
  site_id             uuid NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  carrier_id          uuid NOT NULL REFERENCES carriers(id),
  path                text NOT NULL REFERENCES paths(name),
  underlay_type       text NOT NULL CHECK (underlay_type IN ('fibre', 'broadband', 'lte', 'leo', 'geo')),
  underlay_interface  text NOT NULL,
  underlay_ip         inet,
  commit_mbps         numeric NOT NULL DEFAULT 0,
  cost_per_mbps       numeric NOT NULL DEFAULT 0,
  burst_price         numeric NOT NULL DEFAULT 0,
  UNIQUE (site_id, path)
);

CREATE TABLE IF NOT EXISTS nodes (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id      uuid NOT NULL REFERENCES customers(id),
  site_id          uuid NOT NULL UNIQUE REFERENCES sites(id) ON DELETE CASCADE,
  name             text NOT NULL UNIQUE,
  wg_public_key    text NOT NULL,
  cert_serial      text NOT NULL UNIQUE,
  enrolled_at      timestamptz NOT NULL DEFAULT now(),
  last_seen        timestamptz,
  applied_version  bigint NOT NULL DEFAULT 0,
  apply_ok         boolean,
  apply_error      text,
  agent_version    text
);

CREATE TABLE IF NOT EXISTS enrolment_tokens (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id  uuid NOT NULL REFERENCES customers(id),
  site_id      uuid NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  token_hash   text NOT NULL UNIQUE,
  created_by   text NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  expires_at   timestamptz NOT NULL,
  used_at      timestamptz,
  node_id      uuid
);

CREATE TABLE IF NOT EXISTS app_classes (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id  uuid NOT NULL REFERENCES customers(id),
  name         text NOT NULL,
  description  text NOT NULL DEFAULT '',
  dscp         int[] NOT NULL DEFAULT '{}',
  ports        text NOT NULL DEFAULT '',
  UNIQUE (customer_id, name)
);

CREATE TABLE IF NOT EXISTS sla_policies (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id       uuid NOT NULL REFERENCES customers(id),
  class_name        text NOT NULL,
  max_latency_ms    numeric,
  max_jitter_ms     numeric,
  max_loss_pct      numeric,
  allow_satellite   boolean NOT NULL DEFAULT true,
  UNIQUE (customer_id, class_name)
);

CREATE TABLE IF NOT EXISTS desired_states (
  node_id     uuid NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
  version     bigint NOT NULL,
  body        jsonb NOT NULL,
  body_hash   text NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (node_id, version)
);

CREATE TABLE IF NOT EXISTS users (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email          text NOT NULL UNIQUE,
  password_hash  text NOT NULL,
  role           text NOT NULL CHECK (role IN ('admin', 'customer', 'carrier')),
  customer_id    uuid REFERENCES customers(id),
  carrier_id     uuid REFERENCES carriers(id),
  created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sessions (
  token_hash  text PRIMARY KEY,
  user_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  expires_at  timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
  id           bigserial PRIMARY KEY,
  at           timestamptz NOT NULL DEFAULT now(),
  customer_id  uuid,
  actor        text NOT NULL,
  action       text NOT NULL,
  target       text NOT NULL DEFAULT '',
  detail       jsonb NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS tunnel_state (
  node_id          uuid NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
  tunnel           text NOT NULL,
  path             text NOT NULL,
  handshake_age_s  bigint,
  bfd              text,
  updated_at       timestamptz NOT NULL,
  PRIMARY KEY (node_id, tunnel)
);

-- M4/M5 additions. ALTER ... IF NOT EXISTS keeps existing databases upgradable.
ALTER TABLE customers ADD COLUMN IF NOT EXISTS shadow_mode boolean NOT NULL DEFAULT false;
ALTER TABLE customers ADD COLUMN IF NOT EXISTS storm_mode boolean NOT NULL DEFAULT false;
ALTER TABLE customers ADD COLUMN IF NOT EXISTS storm_since timestamptz;
ALTER TABLE customers ADD COLUMN IF NOT EXISTS storm_by text;
ALTER TABLE customers ADD COLUMN IF NOT EXISTS storm_allow_bulk_sat boolean NOT NULL DEFAULT false;
-- Storm Mode is per site (ADR 0004, revised 2026-10-01). customers.storm_* is kept as a
-- summary: on when any site is on, with the latest switch's time and actor.
ALTER TABLE sites ADD COLUMN IF NOT EXISTS storm_mode boolean NOT NULL DEFAULT false;
ALTER TABLE sites ADD COLUMN IF NOT EXISTS storm_since timestamptz;
ALTER TABLE sites ADD COLUMN IF NOT EXISTS storm_by text;
ALTER TABLE app_classes ADD COLUMN IF NOT EXISTS subnets cidr[] NOT NULL DEFAULT '{}';
ALTER TABLE app_classes ADD COLUMN IF NOT EXISTS ordinal int NOT NULL DEFAULT 100;

-- The routing engine's state per site and class: where the class should be,
-- since when, and the hysteresis memory (CLAUDE.md §4.3).
CREATE TABLE IF NOT EXISTS steering (
  site_id        uuid NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  class_name     text NOT NULL,
  customer_id    uuid NOT NULL REFERENCES customers(id),
  path           text NOT NULL,
  since          timestamptz NOT NULL,
  return_path    text,                 -- a better path being watched for a move back
  return_since   timestamptz,          -- since when it has scored well
  breach_streak  int NOT NULL DEFAULT 0,
  note           text,
  updated_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (site_id, class_name)
);

-- Steering maps sent to agents, versioned like desired_states.
CREATE TABLE IF NOT EXISTS steering_maps (
  node_id     uuid NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
  version     bigint NOT NULL,
  body        jsonb NOT NULL,
  body_hash   text NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (node_id, version)
);

-- Where each agent says each class is right now.
CREATE TABLE IF NOT EXISTS steering_actual (
  node_id     uuid NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
  class_name  text NOT NULL,
  dst         text NOT NULL DEFAULT '',
  path        text,
  paused      boolean NOT NULL DEFAULT false,
  failover    boolean NOT NULL DEFAULT false,
  version     bigint NOT NULL DEFAULT 0,
  updated_at  timestamptz NOT NULL,
  PRIMARY KEY (node_id, class_name, dst)
);

-- Every routing decision with its inputs and a plain-English reason.
CREATE TABLE IF NOT EXISTS decisions (
  id           bigserial PRIMARY KEY,
  time         timestamptz NOT NULL,
  customer_id  uuid NOT NULL REFERENCES customers(id),
  site_id      uuid NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  class_name   text NOT NULL,
  kind         text NOT NULL,          -- move, move_back, failover, hold, storm
  from_path    text,
  to_path      text,
  shadow       boolean NOT NULL DEFAULT false,
  engine       text NOT NULL,
  reason       text NOT NULL,
  inputs       jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS decisions_customer_time ON decisions (customer_id, time DESC);
CREATE INDEX IF NOT EXISTS decisions_site_time ON decisions (site_id, time DESC);

-- Metering (CLAUDE.md §4.5): 5-minute average Mbps per link, the exact
-- samples settlement uses. Rebuilt from iface_counters, so re-running is safe.
CREATE TABLE IF NOT EXISTS usage_5m (
  link_id      uuid NOT NULL REFERENCES links(id) ON DELETE CASCADE,
  bucket       timestamptz NOT NULL,
  customer_id  uuid NOT NULL,
  carrier_id   uuid NOT NULL,
  in_mbps      double precision NOT NULL,
  out_mbps     double precision NOT NULL,
  seconds      double precision NOT NULL,
  PRIMARY KEY (link_id, bucket)
);
CREATE INDEX IF NOT EXISTS usage_5m_carrier ON usage_5m (carrier_id, bucket);

-- Site coordinates, for the hurricane watch (decimal degrees, north and east positive).
ALTER TABLE sites ADD COLUMN IF NOT EXISTS latitude double precision;
ALTER TABLE sites ADD COLUMN IF NOT EXISTS longitude double precision;

-- Insights from the AI features: storm warnings, bill-shock forecasts and
-- carrier anomalies. One open row per key; it is resolved when the condition clears.
CREATE TABLE IF NOT EXISTS insights (
  id               bigserial PRIMARY KEY,
  customer_id      uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  kind             text NOT NULL CHECK (kind IN ('storm_warning', 'bill_shock', 'anomaly')),
  key              text NOT NULL,
  severity         text NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
  site_id          uuid REFERENCES sites(id) ON DELETE CASCADE,
  link_id          uuid REFERENCES links(id) ON DELETE CASCADE,
  carrier_id       uuid REFERENCES carriers(id),
  title            text NOT NULL,
  detail           text NOT NULL,
  data             jsonb NOT NULL DEFAULT '{}',
  example          boolean NOT NULL DEFAULT false,
  first_seen       timestamptz NOT NULL DEFAULT now(),
  last_seen        timestamptz NOT NULL DEFAULT now(),
  resolved_at      timestamptz,
  acknowledged_by  text,
  acknowledged_at  timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS insights_open_key ON insights (customer_id, key) WHERE resolved_at IS NULL;
CREATE INDEX IF NOT EXISTS insights_customer_time ON insights (customer_id, last_seen DESC);
-- Disaster watch (ADR 0009) adds the 'hazard' kind.
ALTER TABLE insights DROP CONSTRAINT IF EXISTS insights_kind_check;
ALTER TABLE insights ADD CONSTRAINT insights_kind_check
  CHECK (kind IN ('storm_warning', 'bill_shock', 'anomaly', 'hazard'));
CREATE INDEX IF NOT EXISTS insights_customer_key ON insights (customer_id, key, resolved_at DESC);

-- Traffic rules and priorities (ADR 0007). A class says how traffic is treated
-- (priority, SLA, preferred path); a rule says which traffic belongs to it.
ALTER TABLE app_classes ADD COLUMN IF NOT EXISTS priority text NOT NULL DEFAULT 'normal';
ALTER TABLE app_classes ADD COLUMN IF NOT EXISTS preferred_path text;
ALTER TABLE app_classes ADD COLUMN IF NOT EXISTS builtin boolean NOT NULL DEFAULT false;
ALTER TABLE links ADD COLUMN IF NOT EXISTS shape_mbps numeric;
ALTER TABLE customers ADD COLUMN IF NOT EXISTS auto_prioritise boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS traffic_rules (
  id             bigserial PRIMARY KEY,
  customer_id    uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  name           text NOT NULL,
  class_name     text NOT NULL,
  site_ids       uuid[] NOT NULL DEFAULT '{}',
  apps           text[] NOT NULL DEFAULT '{}',
  ports          text NOT NULL DEFAULT '',
  dst_subnets    cidr[] NOT NULL DEFAULT '{}',
  src_subnets    cidr[] NOT NULL DEFAULT '{}',
  vlans          int[] NOT NULL DEFAULT '{}',
  domains        text[] NOT NULL DEFAULT '{}',
  dscp           int[] NOT NULL DEFAULT '{}',
  enabled        boolean NOT NULL DEFAULT true,
  ordinal        int NOT NULL DEFAULT 100,
  source         text NOT NULL DEFAULT 'customer' CHECK (source IN ('customer', 'admin', 'detected')),
  created_by     text NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS traffic_rules_customer ON traffic_rules (customer_id, ordinal, id);

-- Application detection: what the agents see leaving each site, and what it looks like.
CREATE TABLE IF NOT EXISTS flow_stats (
  time         timestamptz NOT NULL,
  customer_id  uuid NOT NULL,
  node_id      uuid NOT NULL,
  proto        text NOT NULL,
  dst          inet NOT NULL,
  dport        int NOT NULL,
  class_name   text NOT NULL DEFAULT '',
  flows        int NOT NULL,
  bytes_out    bigint NOT NULL,
  bytes_in     bigint NOT NULL,
  pkts_out     bigint NOT NULL,
  pkts_in      bigint NOT NULL
);
CREATE INDEX IF NOT EXISTS flow_stats_node_time ON flow_stats (node_id, time DESC);

CREATE TABLE IF NOT EXISTS app_detections (
  id               bigserial PRIMARY KEY,
  customer_id      uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  site_id          uuid NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  key              text NOT NULL,
  app_id           text,
  label            text NOT NULL,
  proto            text NOT NULL,
  dport            int NOT NULL,
  dst_subnet       cidr,
  current_class    text NOT NULL DEFAULT '',
  suggested_class  text NOT NULL,
  profile          text NOT NULL,
  confidence       double precision NOT NULL,
  reason           text NOT NULL,
  stats            jsonb NOT NULL DEFAULT '{}',
  status           text NOT NULL DEFAULT 'suggested' CHECK (status IN ('suggested', 'applied', 'dismissed')),
  rule_id          bigint REFERENCES traffic_rules(id) ON DELETE SET NULL,
  first_seen       timestamptz NOT NULL DEFAULT now(),
  last_seen        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (customer_id, site_id, key)
);

-- Time series (TimescaleDB hypertables when the extension is available).
CREATE TABLE IF NOT EXISTS path_metrics (
  time         timestamptz NOT NULL,
  customer_id  uuid NOT NULL,
  node_id      uuid NOT NULL,
  path         text NOT NULL,
  sent         int NOT NULL,
  received     int NOT NULL,
  loss_pct     double precision NOT NULL,
  rtt_avg_ms   double precision,
  rtt_min_ms   double precision,
  rtt_max_ms   double precision,
  jitter_ms    double precision
);
CREATE INDEX IF NOT EXISTS path_metrics_node_time ON path_metrics (node_id, time DESC);

CREATE TABLE IF NOT EXISTS iface_counters (
  time         timestamptz NOT NULL,
  customer_id  uuid NOT NULL,
  node_id      uuid NOT NULL,
  ifname       text NOT NULL,
  rx_bytes     bigint NOT NULL,
  tx_bytes     bigint NOT NULL
);
CREATE INDEX IF NOT EXISTS iface_counters_node_time ON iface_counters (node_id, ifname, time DESC);

CREATE TABLE IF NOT EXISTS events (
  time         timestamptz NOT NULL,
  customer_id  uuid NOT NULL,
  node_id      uuid,
  kind         text NOT NULL,
  detail       jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS events_customer_time ON events (customer_id, time DESC);

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb') THEN
    CREATE EXTENSION IF NOT EXISTS timescaledb;
    PERFORM create_hypertable('path_metrics', 'time', if_not_exists => TRUE, migrate_data => TRUE);
    PERFORM create_hypertable('iface_counters', 'time', if_not_exists => TRUE, migrate_data => TRUE);
    PERFORM create_hypertable('events', 'time', if_not_exists => TRUE, migrate_data => TRUE);
    PERFORM create_hypertable('flow_stats', 'time', if_not_exists => TRUE, migrate_data => TRUE);
  END IF;
END
$$;

INSERT INTO paths (name, label, tunnel, overlay_cidr, pop_port, bfd_profile, local_pref, ordinal) VALUES
  ('carrier-a', 'Carrier A', 'wg-a',   '100.64.1.0/24', 51820, 'terrestrial', 200, 1),
  ('carrier-b', 'Carrier B', 'wg-b',   '100.64.2.0/24', 51821, 'terrestrial', 150, 2),
  ('sat',       'Satellite', 'wg-sat', '100.64.3.0/24', 51822, 'satellite',    50, 3)
ON CONFLICT (name) DO NOTHING;
