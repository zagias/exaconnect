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
  source         text NOT NULL DEFAULT 'customer' CHECK (source IN ('customer', 'admin', 'detected', 'assistant')),
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

-- ExaConnect Fabric: virtual circuits and the cloud router (ADR 0009).
-- A PoP reaches cloud VPN gateways from one interface and address.
ALTER TABLE sites ADD COLUMN IF NOT EXISTS cloud_interface text;
ALTER TABLE sites ADD COLUMN IF NOT EXISTS cloud_address inet;
-- A site's LAN-facing interface, where layer 2 circuits take their VLANs (default eth4).
ALTER TABLE sites ADD COLUMN IF NOT EXISTS lan_interface text;
ALTER TABLE customers ADD COLUMN IF NOT EXISTS cloud_to_cloud boolean NOT NULL DEFAULT true;

CREATE TABLE IF NOT EXISTS circuits (
  id                    bigserial PRIMARY KEY,
  customer_id           uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  name                  text NOT NULL,
  kind                  text NOT NULL CHECK (kind IN ('cloud', 'site')),
  -- cloud: the sites whose subnets the cloud may reach (NULL = all sites);
  -- site: the two ends of a layer 2 circuit and the VLAN at each.
  a_site_id             uuid REFERENCES sites(id) ON DELETE CASCADE,
  a_prefixes            cidr[] NOT NULL DEFAULT '{}',
  a_vlan                int,
  b_site_id             uuid REFERENCES sites(id) ON DELETE CASCADE,
  b_vlan                int,
  -- cloud end: the provider's VPN gateway.
  provider              text,
  region                text NOT NULL DEFAULT '',
  peer_address          inet,
  peer_asn              bigint,
  inside_cidr           cidr,
  psk                   text,
  cloud_prefixes        cidr[] NOT NULL DEFAULT '{}',
  class_name            text,
  bandwidth_mbps        int NOT NULL CHECK (bandwidth_mbps BETWEEN 1 AND 10000),
  price_per_mbps_month  numeric NOT NULL DEFAULT 2.0,
  enabled               boolean NOT NULL DEFAULT true,
  created_by            text NOT NULL,
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now(),
  deleted_at            timestamptz
);
CREATE INDEX IF NOT EXISTS circuits_customer ON circuits (customer_id, id);

-- Elastic bandwidth: one row per speed a circuit has had, for hourly billing.
CREATE TABLE IF NOT EXISTS circuit_bandwidth (
  circuit_id  bigint NOT NULL REFERENCES circuits(id) ON DELETE CASCADE,
  mbps        int NOT NULL,
  valid_from  timestamptz NOT NULL,
  valid_to    timestamptz,
  changed_by  text NOT NULL
);
CREATE INDEX IF NOT EXISTS circuit_bandwidth_circuit ON circuit_bandwidth (circuit_id, valid_from);

CREATE TABLE IF NOT EXISTS circuit_state (
  circuit_id         bigint NOT NULL REFERENCES circuits(id) ON DELETE CASCADE,
  node_id            uuid NOT NULL,
  ike                text NOT NULL DEFAULT '',
  bgp                text NOT NULL DEFAULT '',
  prefixes_received  int NOT NULL DEFAULT 0,
  routes             text[] NOT NULL DEFAULT '{}',
  updated_at         timestamptz NOT NULL,
  PRIMARY KEY (circuit_id, node_id)
);

CREATE TABLE IF NOT EXISTS circuit_metrics (
  time         timestamptz NOT NULL,
  customer_id  uuid NOT NULL,
  circuit_id   bigint NOT NULL,
  node_id      uuid NOT NULL,
  sent         int NOT NULL,
  received     int NOT NULL,
  rtt_avg_ms   double precision,
  -- the circuit interface's cumulative counters on this node
  bytes_in     bigint NOT NULL DEFAULT 0,
  bytes_out    bigint NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS circuit_metrics_circuit_time ON circuit_metrics (circuit_id, time DESC);

-- Internet breakout, NAT gateway and firewall (ADR 0010).
-- Per site: through the PoP (default), straight out of its own links, or off.
ALTER TABLE sites ADD COLUMN IF NOT EXISTS internet_mode text NOT NULL DEFAULT 'pop';
DO $$ BEGIN
  ALTER TABLE sites ADD CONSTRAINT sites_internet_mode_check CHECK (internet_mode IN ('pop', 'local', 'off'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
-- The PoP's interface and next hop toward the internet.
ALTER TABLE sites ADD COLUMN IF NOT EXISTS internet_interface text;
ALTER TABLE sites ADD COLUMN IF NOT EXISTS internet_gateway inet;
-- A site's next hop on each carrier link, for local breakout.
ALTER TABLE links ADD COLUMN IF NOT EXISTS underlay_gateway inet;

CREATE TABLE IF NOT EXISTS firewall_rules (
  id           bigserial PRIMARY KEY,
  customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  site_id      uuid REFERENCES sites(id) ON DELETE CASCADE,  -- NULL = every site
  position     int NOT NULL,
  action       text NOT NULL CHECK (action IN ('allow', 'deny')),
  src          cidr[] NOT NULL DEFAULT '{}',
  dst          cidr[] NOT NULL DEFAULT '{}',
  protocol     text NOT NULL DEFAULT 'any' CHECK (protocol IN ('any', 'tcp', 'udp', 'icmp')),
  ports        text NOT NULL DEFAULT '',
  description  text NOT NULL DEFAULT '',
  enabled      boolean NOT NULL DEFAULT true,
  created_by   text NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS firewall_rules_customer ON firewall_rules (customer_id, position);

CREATE TABLE IF NOT EXISTS port_forwards (
  id           bigserial PRIMARY KEY,
  customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  description  text NOT NULL DEFAULT '',
  protocol     text NOT NULL CHECK (protocol IN ('tcp', 'udp')),
  port         int NOT NULL CHECK (port BETWEEN 1 AND 65535),
  to_site_id   uuid NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  to_address   inet NOT NULL,
  to_port      int NOT NULL CHECK (to_port BETWEEN 1 AND 65535),
  allow_from   cidr[] NOT NULL DEFAULT '{}',
  enabled      boolean NOT NULL DEFAULT true,
  created_by   text NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now(),
  -- the PoP has one public address, shared by every customer
  UNIQUE (protocol, port)
);

-- What each node last reported: where internet traffic leaves, and nft's
-- cumulative counters per rule, port forward and the inbound drop.
CREATE TABLE IF NOT EXISTS internet_state (
  node_id     uuid PRIMARY KEY,
  customer_id uuid NOT NULL,
  mode        text NOT NULL DEFAULT '',
  via         text NOT NULL DEFAULT '',
  counters    jsonb NOT NULL DEFAULT '[]',
  updated_at  timestamptz NOT NULL
);

-- Partner directory and plain-English ordering (ADR 0011).
CREATE TABLE IF NOT EXISTS partners (
  id                    bigserial PRIMARY KEY,
  slug                  text NOT NULL UNIQUE,
  name                  text NOT NULL,
  category              text NOT NULL CHECK (category IN ('cloud', 'saas', 'payments', 'internet', 'security',
                                                          'content', 'other')),
  kind                  text NOT NULL CHECK (kind IN ('cloud', 'service')),
  provider              text,
  description           text NOT NULL DEFAULT '',
  website               text NOT NULL DEFAULT '',
  regions               text[] NOT NULL DEFAULT '{}',
  prefixes              cidr[] NOT NULL DEFAULT '{}',
  price_per_mbps_month  numeric NOT NULL DEFAULT 2.0,
  listed                boolean NOT NULL DEFAULT true,
  example               boolean NOT NULL DEFAULT false,
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE circuits ADD COLUMN IF NOT EXISTS partner_id bigint REFERENCES partners(id);

CREATE TABLE IF NOT EXISTS orders (
  id            bigserial PRIMARY KEY,
  customer_id   uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  status        text NOT NULL DEFAULT 'draft'
                CHECK (status IN ('draft', 'done', 'pending_partner', 'cancelled', 'failed')),
  engine        text NOT NULL CHECK (engine IN ('ai', 'rules', 'form')),
  text          text NOT NULL DEFAULT '',
  actions       jsonb NOT NULL DEFAULT '[]',
  results       jsonb NOT NULL DEFAULT '[]',
  created_by    text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  confirmed_by  text,
  confirmed_at  timestamptz
);
CREATE INDEX IF NOT EXISTS orders_customer ON orders (customer_id, id DESC);
CREATE INDEX IF NOT EXISTS orders_status ON orders (status) WHERE status = 'pending_partner';

-- Rules the assistant adds are marked as its own (older databases lack the value).
ALTER TABLE traffic_rules DROP CONSTRAINT IF EXISTS traffic_rules_source_check;
ALTER TABLE traffic_rules ADD CONSTRAINT traffic_rules_source_check
  CHECK (source IN ('customer', 'admin', 'detected', 'assistant'));

-- Changes the assistant proposed from Ask (ADR 0015). Nothing applies until a
-- person confirms; each applied action keeps what is needed to undo it.
CREATE TABLE IF NOT EXISTS assistant_plans (
  id           bigserial PRIMARY KEY,
  customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  status       text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'applied', 'undone', 'cancelled')),
  question     text NOT NULL DEFAULT '',
  answer       text NOT NULL DEFAULT '',
  actions      jsonb NOT NULL DEFAULT '[]',
  results      jsonb NOT NULL DEFAULT '[]',
  created_by   text NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  applied_by   text,
  applied_at   timestamptz,
  undone_by    text,
  undone_at    timestamptz
);
CREATE INDEX IF NOT EXISTS assistant_plans_customer ON assistant_plans (customer_id, id DESC);

-- The directory starts with the clouds ExaConnect connects to, plus two
-- clearly marked example service partners. Admins edit or unlist them.
INSERT INTO partners (slug, name, category, kind, provider, description, website, regions, prefixes, example) VALUES
  ('aws', 'Amazon Web Services', 'cloud', 'cloud', 'aws',
   'Private connection to your VPCs over a Site-to-Site VPN, with BGP.', 'https://aws.amazon.com',
   '{us-east-1,us-east-2,us-west-2,sa-east-1,ca-central-1,eu-west-2}', '{}', false),
  ('azure', 'Microsoft Azure', 'cloud', 'cloud', 'azure',
   'Private connection to your virtual networks through a VPN gateway, with BGP.', 'https://azure.microsoft.com',
   '{eastus,eastus2,southcentralus,brazilsouth,canadacentral,uksouth}', '{}', false),
  ('google-cloud', 'Google Cloud', 'cloud', 'cloud', 'gcp',
   'Private connection to your VPC networks through HA VPN and Cloud Router.', 'https://cloud.google.com',
   '{us-east1,us-east4,us-central1,southamerica-east1,northamerica-northeast1}', '{}', false),
  ('oracle-cloud', 'Oracle Cloud', 'cloud', 'cloud', 'oracle',
   'Private connection to your VCNs over Site-to-Site VPN, with BGP.', 'https://www.oracle.com/cloud',
   '{us-ashburn-1,us-phoenix-1,sa-saopaulo-1,ca-toronto-1}', '{}', false),
  ('example-payments', 'Example Payments Network', 'payments', 'service', NULL,
   'Sample entry: a card payments network reached privately from your sites.', '',
   '{}', '{203.0.113.0/25}', true),
  ('example-erp', 'Example ERP Service', 'saas', 'service', NULL,
   'Sample entry: a hosted ERP service reached privately from your sites.', '',
   '{}', '{203.0.113.128/25}', true)
ON CONFLICT (slug) DO NOTHING;

-- Resilient circuits, DDoS protection and encryption reporting (ADR 0012).
ALTER TABLE circuits ADD COLUMN IF NOT EXISTS secondary_peer_address inet;
ALTER TABLE circuits ADD COLUMN IF NOT EXISTS secondary_inside_cidr cidr;
ALTER TABLE circuit_state ADD COLUMN IF NOT EXISTS tunnel smallint NOT NULL DEFAULT 1;
ALTER TABLE circuit_state ADD COLUMN IF NOT EXISTS ike_cipher text NOT NULL DEFAULT '';
ALTER TABLE circuit_state ADD COLUMN IF NOT EXISTS esp_cipher text NOT NULL DEFAULT '';
ALTER TABLE circuit_state ADD COLUMN IF NOT EXISTS established_s int NOT NULL DEFAULT -1;
DO $$
BEGIN
  IF (SELECT array_length(conkey, 1) FROM pg_constraint WHERE conname = 'circuit_state_pkey') = 2 THEN
    ALTER TABLE circuit_state DROP CONSTRAINT circuit_state_pkey;
    ALTER TABLE circuit_state ADD PRIMARY KEY (circuit_id, node_id, tunnel);
  END IF;
END $$;
ALTER TABLE circuit_metrics ADD COLUMN IF NOT EXISTS tunnel smallint NOT NULL DEFAULT 1;

ALTER TABLE sites ADD COLUMN IF NOT EXISTS ddos_enabled boolean NOT NULL DEFAULT true;
ALTER TABLE sites ADD COLUMN IF NOT EXISTS ddos_new_per_source int NOT NULL DEFAULT 50;
ALTER TABLE sites ADD COLUMN IF NOT EXISTS ddos_syn_per_s int NOT NULL DEFAULT 2000;
ALTER TABLE sites ADD COLUMN IF NOT EXISTS ddos_block_minutes int NOT NULL DEFAULT 10;
ALTER TABLE internet_state ADD COLUMN IF NOT EXISTS auto_blocked jsonb NOT NULL DEFAULT '[]';
CREATE TABLE IF NOT EXISTS blocked_sources (
  id          bigserial PRIMARY KEY,
  prefix      cidr NOT NULL,
  reason      text NOT NULL DEFAULT '',
  created_by  text NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  expires_at  timestamptz
);
ALTER TABLE blocked_sources ADD COLUMN IF NOT EXISTS lifted boolean NOT NULL DEFAULT false;

-- API keys for automation: the SDK, Terraform, customers' own code (ADR 0013).
CREATE TABLE IF NOT EXISTS api_keys (
  id            bigserial PRIMARY KEY,
  user_id       uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name          text NOT NULL,
  prefix        text NOT NULL,
  token_hash    text NOT NULL UNIQUE,
  created_at    timestamptz NOT NULL DEFAULT now(),
  last_used_at  timestamptz,
  expires_at    timestamptz,
  revoked_at    timestamptz
);
CREATE INDEX IF NOT EXISTS api_keys_user ON api_keys (user_id) WHERE revoked_at IS NULL;

-- Billing phase 1 (ADR 0022): plans per product, versioned price lists,
-- subscriptions, draft and issued invoices, payments.
--
-- Connect and CommAI are sold as separate plans; an organisation (customer)
-- may hold either or both. Subscriptions are the source of truth for which
-- products an organisation holds (billing/plans.py keeps customers.products,
-- when that column exists, in step with them).
CREATE TABLE IF NOT EXISTS plans (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  product         text NOT NULL CHECK (product IN ('connect', 'commai')),
  name            text NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
  description     text NOT NULL DEFAULT '',
  billing_period  text NOT NULL DEFAULT 'month' CHECK (billing_period IN ('month')),
  active          boolean NOT NULL DEFAULT true,
  example         boolean NOT NULL DEFAULT false,
  created_by      text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (product, name)
);
INSERT INTO plans (product, name, description, example, created_by) VALUES
  ('connect', 'Connect Standard', 'Sites, carrier links, Storm Mode and Fabric.', true, 'system:schema'),
  ('commai', 'CommAI Standard', 'Inbox, channels, AI agents and voice.', true, 'system:schema')
ON CONFLICT (product, name) DO NOTHING;

-- A plan's prices. Every save is a new version with the date it takes effect;
-- old versions are never changed, so an invoice can always be explained.
-- customer_id NULL: the plan's list for everyone; set: one organisation's own
-- prices on that plan (a negotiated contract), which win over the plan's list.
-- Connect uses the site, link, satellite and circuit prices; CommAI uses
-- meter_prices. Both have the monthly plan fee, currency, tax and SLA credits.
CREATE TABLE IF NOT EXISTS price_lists (
  id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  plan_id                 uuid NOT NULL REFERENCES plans(id),
  customer_id             uuid REFERENCES customers(id) ON DELETE CASCADE,
  version                 int NOT NULL,
  effective_from          date NOT NULL,
  label                   text NOT NULL DEFAULT '',
  currency                text NOT NULL DEFAULT 'USD' CHECK (currency ~ '^[A-Z]{3}$'),
  tax_rate_pct            numeric(6, 3) NOT NULL DEFAULT 0 CHECK (tax_rate_pct BETWEEN 0 AND 100),
  monthly_fee             numeric(14, 4) NOT NULL DEFAULT 0 CHECK (monthly_fee >= 0),
  site_monthly            numeric(14, 4) NOT NULL DEFAULT 0 CHECK (site_monthly >= 0),
  commit_per_mbps         numeric(14, 4) NOT NULL DEFAULT 0 CHECK (commit_per_mbps >= 0),
  burst_per_mbps          numeric(14, 4) NOT NULL DEFAULT 0 CHECK (burst_per_mbps >= 0),
  satellite_per_gb        numeric(14, 4) NOT NULL DEFAULT 0 CHECK (satellite_per_gb >= 0),
  -- NULL: each virtual circuit's own price per Mbps a month (fabric.py).
  circuit_per_mbps_month  numeric(14, 4) CHECK (circuit_per_mbps_month >= 0),
  -- CommAI: {"ai_reply": "0.02", "message_out:whatsapp": "0.01", ...} per unit.
  meter_prices            jsonb NOT NULL DEFAULT '{}',
  -- [{"below_pct": 99.9, "credit_pct": 5}, ...]: credit as a share of the site's monthly fee.
  sla_credits             jsonb NOT NULL DEFAULT '[]',
  credit_cap_pct          numeric(6, 3) NOT NULL DEFAULT 50 CHECK (credit_cap_pct BETWEEN 0 AND 100),
  example                 boolean NOT NULL DEFAULT false,
  created_by              text NOT NULL,
  created_at              timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS price_lists_version
  ON price_lists (plan_id, COALESCE(customer_id, '00000000-0000-0000-0000-000000000000'::uuid), version);

-- Each plan starts with a clearly marked example list until ExaCarib agrees prices.
INSERT INTO price_lists (plan_id, version, effective_from, label, site_monthly, commit_per_mbps, burst_per_mbps,
                         satellite_per_gb, sla_credits, credit_cap_pct, example, created_by)
SELECT p.id, 1, DATE '2026-01-01', 'Example prices (not real prices)', 150, 6, 9, 2.5,
       '[{"below_pct": 99.9, "credit_pct": 5}, {"below_pct": 99.5, "credit_pct": 10},
         {"below_pct": 99, "credit_pct": 25}]', 50, true, 'system:schema'
FROM plans p WHERE p.product = 'connect' AND p.name = 'Connect Standard'
  AND NOT EXISTS (SELECT 1 FROM price_lists x WHERE x.plan_id = p.id AND x.customer_id IS NULL);
INSERT INTO price_lists (plan_id, version, effective_from, label, monthly_fee, meter_prices, example, created_by)
SELECT p.id, 1, DATE '2026-01-01', 'Example prices (not real prices)', 49,
       '{"ai_reply": "0.02", "ai_tokens": "0", "copilot": "0.01", "message_out:whatsapp": "0.01",
         "message_out:sms": "0.02", "message_out:email": "0"}', true, 'system:schema'
FROM plans p WHERE p.product = 'commai' AND p.name = 'CommAI Standard'
  AND NOT EXISTS (SELECT 1 FROM price_lists x WHERE x.plan_id = p.id AND x.customer_id IS NULL);

-- An organisation's plans. ends_on is exclusive; NULL means open-ended. A plan
-- change ends one subscription on a day and starts the next on the same day.
CREATE TABLE IF NOT EXISTS subscriptions (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  plan_id      uuid NOT NULL REFERENCES plans(id),
  product      text NOT NULL CHECK (product IN ('connect', 'commai')),
  starts_on    date NOT NULL,
  ends_on      date,
  status       text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'ended')),
  created_by   text NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  ended_by     text,
  ended_at     timestamptz,
  CHECK (ends_on IS NULL OR ends_on > starts_on),
  CHECK ((status = 'ended') = (ends_on IS NOT NULL))
);
-- At most one open-ended subscription per organisation and product.
CREATE UNIQUE INDEX IF NOT EXISTS subscriptions_open
  ON subscriptions (customer_id, product) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS subscriptions_customer ON subscriptions (customer_id, product, starts_on);

-- Organisations already using Connect before plans existed keep it: one
-- subscription each, from the day they were added. Only where none was ever made.
INSERT INTO subscriptions (customer_id, plan_id, product, starts_on, created_by)
SELECT c.id, p.id, 'connect', c.created_at::date, 'system:schema'
FROM customers c JOIN plans p ON p.product = 'connect' AND p.name = 'Connect Standard'
WHERE EXISTS (SELECT 1 FROM sites s WHERE s.customer_id = c.id)
  AND NOT EXISTS (SELECT 1 FROM subscriptions x WHERE x.customer_id = c.id AND x.product = 'connect');

-- One invoice per subscription and calendar month (UTC). The period is the
-- part of the month the subscription covered.
CREATE TABLE IF NOT EXISTS billing_invoices (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id      uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  product          text NOT NULL CHECK (product IN ('connect', 'commai')),
  subscription_id  uuid NOT NULL REFERENCES subscriptions(id),
  plan_id          uuid NOT NULL REFERENCES plans(id),
  plan             text NOT NULL,                -- the plan's name when drafted
  period_start     date NOT NULL,                -- the first day of the month
  period_end       date NOT NULL,                -- exclusive: the first day of the next month
  covered_from     date NOT NULL,                -- the subscription's part of the month
  covered_to       date NOT NULL,                -- exclusive
  status           text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'issued', 'void')),
  number           text UNIQUE,                  -- EXA-2026-0001, set when issued
  price_list_id    uuid NOT NULL REFERENCES price_lists(id),
  currency         text NOT NULL,
  charges          numeric(14, 2) NOT NULL DEFAULT 0,
  credits          numeric(14, 2) NOT NULL DEFAULT 0,   -- zero or negative
  subtotal         numeric(14, 2) NOT NULL DEFAULT 0,
  tax_rate_pct     numeric(6, 3) NOT NULL DEFAULT 0,
  tax              numeric(14, 2) NOT NULL DEFAULT 0,
  total            numeric(14, 2) NOT NULL DEFAULT 0,
  example          boolean NOT NULL DEFAULT false,
  created_by       text NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now(),
  generated_at     timestamptz NOT NULL DEFAULT now(),
  issued_by        text,
  issued_at        timestamptz,
  voided_by        text,
  voided_at        timestamptz,
  void_reason      text
);
-- One live invoice (draft or issued) per subscription and month; voiding one frees it.
CREATE UNIQUE INDEX IF NOT EXISTS billing_invoices_live
  ON billing_invoices (subscription_id, period_start) WHERE status IN ('draft', 'issued');
CREATE INDEX IF NOT EXISTS billing_invoices_customer ON billing_invoices (customer_id, period_start DESC);

CREATE TABLE IF NOT EXISTS billing_invoice_lines (
  id           bigserial PRIMARY KEY,
  invoice_id   uuid NOT NULL REFERENCES billing_invoices(id) ON DELETE CASCADE,
  customer_id  uuid NOT NULL,
  position     int NOT NULL,
  product      text NOT NULL,
  plan         text NOT NULL,                -- the plan's name: every line names its plan
  kind         text NOT NULL CHECK (kind IN ('plan', 'site', 'commit', 'burst', 'satellite', 'circuit', 'credit',
                                             'usage', 'voice')),
  description  text NOT NULL,
  site_id      uuid,
  link_id      uuid,
  circuit_id   bigint,
  class_name   text,
  quantity     numeric(18, 6) NOT NULL,
  unit         text NOT NULL,
  unit_price   numeric(14, 6) NOT NULL,
  amount       numeric(14, 2) NOT NULL,
  inputs       jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS billing_invoice_lines_invoice ON billing_invoice_lines (invoice_id, position);

-- Issued and void invoices never change: an issued one may only become void, and
-- lines change only while their invoice is a draft.
CREATE OR REPLACE FUNCTION billing_invoice_frozen() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF EXISTS (SELECT 1 FROM billing_invoices WHERE id = NEW.invoice_id AND status <> 'draft') THEN
      RAISE EXCEPTION 'lines of an issued invoice are frozen' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
  END IF;
  -- Removing the whole customer (cascade) is the one exception.
  IF TG_OP = 'DELETE' AND NOT EXISTS (SELECT 1 FROM customers WHERE id = OLD.customer_id) THEN
    RETURN OLD;
  END IF;
  IF TG_TABLE_NAME = 'billing_invoices' THEN
    IF OLD.status = 'void' THEN
      RAISE EXCEPTION 'invoice % is void and frozen', COALESCE(OLD.number, OLD.id::text)
        USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.status = 'issued' AND (TG_OP = 'DELETE' OR NEW.status <> 'void'
        OR (NEW.number, NEW.customer_id, NEW.product, NEW.subscription_id, NEW.plan_id, NEW.plan,
            NEW.period_start, NEW.period_end, NEW.covered_from, NEW.covered_to, NEW.price_list_id, NEW.currency,
            NEW.charges, NEW.credits, NEW.subtotal, NEW.tax_rate_pct, NEW.tax, NEW.total, NEW.issued_by,
            NEW.issued_at, NEW.generated_at)
           IS DISTINCT FROM
           (OLD.number, OLD.customer_id, OLD.product, OLD.subscription_id, OLD.plan_id, OLD.plan,
            OLD.period_start, OLD.period_end, OLD.covered_from, OLD.covered_to, OLD.price_list_id, OLD.currency,
            OLD.charges, OLD.credits, OLD.subtotal, OLD.tax_rate_pct, OLD.tax, OLD.total, OLD.issued_by,
            OLD.issued_at, OLD.generated_at)) THEN
      RAISE EXCEPTION 'invoice % is issued and frozen', OLD.number USING ERRCODE = 'check_violation';
    END IF;
  ELSE
    IF EXISTS (SELECT 1 FROM billing_invoices WHERE id = OLD.invoice_id AND status <> 'draft') THEN
      RAISE EXCEPTION 'lines of an issued invoice are frozen' USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS billing_invoices_frozen ON billing_invoices;
CREATE TRIGGER billing_invoices_frozen BEFORE UPDATE OR DELETE ON billing_invoices
  FOR EACH ROW EXECUTE FUNCTION billing_invoice_frozen();
DROP TRIGGER IF EXISTS billing_invoice_lines_frozen ON billing_invoice_lines;
CREATE TRIGGER billing_invoice_lines_frozen BEFORE INSERT OR UPDATE OR DELETE ON billing_invoice_lines
  FOR EACH ROW EXECUTE FUNCTION billing_invoice_frozen();

-- Online payment of issued invoices (off by default; ADR 0022). A payment never
-- changes its invoice: the invoice is paid when a payment for it is 'paid', and
-- only a correctly signed gateway webhook sets that.
CREATE TABLE IF NOT EXISTS billing_payments (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  invoice_id   uuid NOT NULL REFERENCES billing_invoices(id) ON DELETE CASCADE,
  customer_id  uuid NOT NULL,
  provider     text NOT NULL CHECK (provider IN ('stripe', 'hosted')),
  mode         text NOT NULL CHECK (mode IN ('simulated', 'live')),
  reference    text,
  url          text,
  amount       numeric(14, 2) NOT NULL,
  currency     text NOT NULL,
  status       text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'paid', 'failed', 'mismatch')),
  event_id     text,
  detail       jsonb NOT NULL DEFAULT '{}',
  created_by   text NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  paid_at      timestamptz
);
CREATE INDEX IF NOT EXISTS billing_payments_invoice ON billing_payments (invoice_id, created_at DESC);
-- Each gateway event is acted on once, however often it is delivered.
CREATE TABLE IF NOT EXISTS billing_payment_events (
  provider     text NOT NULL,
  event_id     text NOT NULL,
  received_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (provider, event_id)
);

-- Supplier side: what ExaCarib pays a Fabric partner per Mbps a month for a circuit
-- (NULL: not agreed yet, shown as unknown rather than zero).
ALTER TABLE partners ADD COLUMN IF NOT EXISTS cost_per_mbps_month numeric(14, 4);

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
    PERFORM create_hypertable('circuit_metrics', 'time', if_not_exists => TRUE, migrate_data => TRUE);
    -- Retention: the server's disk is finite. Probe and circuit samples are kept 90 days;
    -- interface counters and events 13 months, so a full billing year and its disputes
    -- can be re-run. Metering's 5-minute samples (usage_5m) are not dropped.
    BEGIN
      PERFORM add_retention_policy('path_metrics', INTERVAL '90 days', if_not_exists => TRUE);
      PERFORM add_retention_policy('circuit_metrics', INTERVAL '90 days', if_not_exists => TRUE);
      PERFORM add_retention_policy('iface_counters', INTERVAL '400 days', if_not_exists => TRUE);
      PERFORM add_retention_policy('events', INTERVAL '400 days', if_not_exists => TRUE);
    EXCEPTION WHEN OTHERS THEN
      RAISE NOTICE 'retention policies not added: %', SQLERRM;
    END;
  END IF;
END
$$;

INSERT INTO paths (name, label, tunnel, overlay_cidr, pop_port, bfd_profile, local_pref, ordinal) VALUES
  ('carrier-a', 'Carrier A', 'wg-a',   '100.64.1.0/24', 51820, 'terrestrial', 200, 1),
  ('carrier-b', 'Carrier B', 'wg-b',   '100.64.2.0/24', 51821, 'terrestrial', 150, 2),
  ('sat',       'Satellite', 'wg-sat', '100.64.3.0/24', 51822, 'satellite',    50, 3)
ON CONFLICT (name) DO NOTHING;

-- Releases (ADR 0025): written by deploy/release/release.sh on the host, read by Admin > Releases.
CREATE TABLE IF NOT EXISTS releases (
  id           bigserial PRIMARY KEY,
  commit       text NOT NULL,
  previous     text,
  started_at   timestamptz NOT NULL DEFAULT now(),
  finished_at  timestamptz,
  status       text NOT NULL CHECK (status IN ('deploying', 'live', 'rolled_back', 'failed')),
  kind         text NOT NULL DEFAULT 'release' CHECK (kind IN ('release', 'rollback')),
  backup       text,
  detail       text NOT NULL DEFAULT ''
);

-- Lab and demo organisations: every screen showing their data carries "Example data" (CLAUDE.md 4.6).
ALTER TABLE customers ADD COLUMN IF NOT EXISTS example boolean NOT NULL DEFAULT false;

-- Forgotten-password links (api/password_reset.py). Only the hash is stored.
CREATE TABLE IF NOT EXISTS password_resets (
  id          bigserial PRIMARY KEY,
  user_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash  text NOT NULL UNIQUE,
  ip          text,
  created_at  timestamptz NOT NULL DEFAULT now(),
  expires_at  timestamptz NOT NULL,
  used_at     timestamptz
);
CREATE INDEX IF NOT EXISTS password_resets_user ON password_resets (user_id, created_at DESC);
