-- Commercial (ADR 0055): product catalogue, rating, quotes (CPQ), sales orders,
-- contract items, one-off charges and usage events. Applied after schema.sql.
--
-- What is billed where:
--   * plan fees, sites and link commits stay on the plan's price list (ADR 0022);
--     catalogue items for them ("price_source" not 'catalogue') only quote and
--     fulfil them, and a quoted discount becomes a recurring credit item here;
--   * everything else sold through a quote is billed from contract_items
--     (recurring and usage) and one_off_charges, on the plan invoice of the
--     item's product.

CREATE TABLE IF NOT EXISTS catalogue_items (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  code              text NOT NULL UNIQUE CHECK (code ~ '^[a-z0-9][a-z0-9._-]{1,47}$'),
  name              text NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
  description       text NOT NULL DEFAULT '',
  product           text NOT NULL CHECK (product IN ('connect', 'commai')),
  family            text NOT NULL DEFAULT 'service'
                    CHECK (family IN ('plan', 'site', 'link', 'hardware', 'service', 'seat', 'support', 'usage')),
  charge_type       text NOT NULL CHECK (charge_type IN ('recurring', 'one_off', 'usage')),
  unit              text NOT NULL DEFAULT 'each' CHECK (length(unit) BETWEEN 1 AND 30),
  -- usage items: the meter their usage events carry.
  meter             text CHECK (meter IS NULL OR meter ~ '^[a-z0-9][a-z0-9._:-]{0,63}$'),
  -- where the price comes from: this catalogue, or the organisation's plan price list.
  price_source      text NOT NULL DEFAULT 'catalogue'
                    CHECK (price_source IN ('catalogue', 'plan_fee', 'site_monthly', 'commit_per_mbps')),
  -- what accepting an order for it does.
  fulfilment        text NOT NULL DEFAULT 'manual'
                    CHECK (fulfilment IN ('immediate', 'manual', 'subscription', 'site', 'link')),
  plan_id           uuid REFERENCES plans(id),
  max_discount_pct  numeric(6, 3) NOT NULL DEFAULT 10 CHECK (max_discount_pct BETWEEN 0 AND 100),
  active            boolean NOT NULL DEFAULT true,
  example           boolean NOT NULL DEFAULT false,
  position          int NOT NULL DEFAULT 100,
  created_by        text NOT NULL,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  CHECK ((charge_type = 'usage') = (meter IS NOT NULL)),
  CHECK (price_source = 'catalogue' OR charge_type = 'recurring'),
  CHECK ((fulfilment = 'subscription') = (plan_id IS NOT NULL))
);

-- Versioned prices. A new version never changes an old one; quotes keep the
-- version and a copy of the price they used.
CREATE TABLE IF NOT EXISTS catalogue_prices (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  item_id         uuid NOT NULL REFERENCES catalogue_items(id) ON DELETE CASCADE,
  version         int NOT NULL,
  currency        text NOT NULL DEFAULT 'USD' CHECK (currency ~ '^[A-Z]{3}$'),
  effective_from  date NOT NULL,
  model           text NOT NULL DEFAULT 'per_unit' CHECK (model IN ('per_unit', 'flat', 'tiered', 'volume')),
  unit_price      numeric(14, 4) NOT NULL DEFAULT 0 CHECK (unit_price >= 0),
  -- tiered/volume: [{"up_to": 10, "unit_price": "5"}, {"up_to": null, "unit_price": "4"}]
  tiers           jsonb NOT NULL DEFAULT '[]',
  included_qty    numeric(18, 6) NOT NULL DEFAULT 0 CHECK (included_qty >= 0),
  minimum         numeric(14, 4) NOT NULL DEFAULT 0 CHECK (minimum >= 0),
  -- recurring: [{"min_months": 12, "pct": 5}, {"min_months": 36, "pct": 10}]
  term_discounts  jsonb NOT NULL DEFAULT '[]',
  example         boolean NOT NULL DEFAULT false,
  created_by      text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (item_id, currency, version)
);

CREATE TABLE IF NOT EXISTS quotes (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  number           text NOT NULL UNIQUE,              -- Q-2026-0001
  customer_id      uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  title            text NOT NULL DEFAULT '' CHECK (length(title) <= 120),
  status           text NOT NULL DEFAULT 'draft'
                   CHECK (status IN ('draft', 'pending_approval', 'approved', 'sent', 'accepted', 'declined',
                                     'cancelled')),
  currency         text NOT NULL DEFAULT 'USD' CHECK (currency ~ '^[A-Z]{3}$'),
  term_months      int NOT NULL DEFAULT 12 CHECK (term_months BETWEEN 1 AND 120),
  valid_until      date NOT NULL,
  notes            text NOT NULL DEFAULT '' CHECK (length(notes) <= 4000),
  one_off_total    numeric(14, 2) NOT NULL DEFAULT 0,
  monthly_total    numeric(14, 2) NOT NULL DEFAULT 0,
  contract_total   numeric(14, 2) NOT NULL DEFAULT 0,  -- one-off + monthly x term (usage not included)
  needs_approval   boolean NOT NULL DEFAULT false,
  example          boolean NOT NULL DEFAULT false,
  created_by       text NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  approved_by      text,
  approved_at      timestamptz,
  approval_note    text,
  sent_by          text,
  sent_at          timestamptz,
  accepted_by      text,
  accepted_name    text,
  accepted_at      timestamptz,
  declined_by      text,
  declined_at      timestamptz,
  decline_reason   text,
  cancelled_by     text,
  cancelled_at     timestamptz
);
CREATE INDEX IF NOT EXISTS quotes_customer ON quotes (customer_id, created_at DESC);

CREATE TABLE IF NOT EXISTS quote_lines (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  quote_id          uuid NOT NULL REFERENCES quotes(id) ON DELETE CASCADE,
  position          int NOT NULL,
  item_id           uuid NOT NULL REFERENCES catalogue_items(id),
  price_id          uuid REFERENCES catalogue_prices(id),  -- NULL: priced from the plan price list
  code              text NOT NULL,
  name              text NOT NULL,
  product           text NOT NULL,
  charge_type       text NOT NULL,
  unit              text NOT NULL,
  quantity          numeric(18, 6) NOT NULL CHECK (quantity > 0),
  config            jsonb NOT NULL DEFAULT '{}',
  description       text NOT NULL DEFAULT '',
  list_amount       numeric(14, 4) NOT NULL DEFAULT 0,     -- before discounts, per month or once
  term_discount_pct numeric(6, 3) NOT NULL DEFAULT 0,
  discount_pct      numeric(6, 3) NOT NULL DEFAULT 0 CHECK (discount_pct BETWEEN 0 AND 100),
  amount            numeric(14, 4) NOT NULL DEFAULT 0,     -- after discounts, per month or once
  price             jsonb NOT NULL DEFAULT '{}',           -- the price used (snapshot)
  rating            jsonb NOT NULL DEFAULT '{}',           -- the rating engine's breakdown
  UNIQUE (quote_id, position)
);

CREATE TABLE IF NOT EXISTS sales_orders (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  number        text NOT NULL UNIQUE,                    -- SO-2026-0001
  customer_id   uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  quote_id      uuid UNIQUE REFERENCES quotes(id),
  status        text NOT NULL DEFAULT 'acknowledged'
                CHECK (status IN ('acknowledged', 'in_progress', 'completed', 'partial', 'cancelled')),
  currency      text NOT NULL,
  term_months   int NOT NULL,
  created_by    text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  completed_at  timestamptz,
  cancelled_by  text,
  cancelled_at  timestamptz,
  cancel_reason text
);
CREATE INDEX IF NOT EXISTS sales_orders_customer ON sales_orders (customer_id, created_at DESC);

CREATE TABLE IF NOT EXISTS sales_order_items (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  order_id       uuid NOT NULL REFERENCES sales_orders(id) ON DELETE CASCADE,
  customer_id    uuid NOT NULL,
  position       int NOT NULL,
  quote_line_id  uuid REFERENCES quote_lines(id),
  item_id        uuid NOT NULL REFERENCES catalogue_items(id),
  code           text NOT NULL,
  name           text NOT NULL,
  product        text NOT NULL,
  charge_type    text NOT NULL,
  fulfilment     text NOT NULL,
  quantity       numeric(18, 6) NOT NULL,
  config         jsonb NOT NULL DEFAULT '{}',
  description    text NOT NULL DEFAULT '',
  amount         numeric(14, 4) NOT NULL DEFAULT 0,
  list_amount    numeric(14, 4) NOT NULL DEFAULT 0,
  price          jsonb NOT NULL DEFAULT '{}',
  discount       jsonb NOT NULL DEFAULT '{}',
  status         text NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'done', 'failed', 'cancelled')),
  result         jsonb NOT NULL DEFAULT '{}',
  error          text,
  done_by        text,
  done_at        timestamptz,
  UNIQUE (order_id, position)
);
CREATE INDEX IF NOT EXISTS sales_order_items_open ON sales_order_items (order_id) WHERE status = 'pending';

-- What an organisation has bought and pays for every month (or by use).
-- unit_price is per month for recurring items, after discounts; a negative one
-- is a recurring credit (a discount on something the plan price list bills).
CREATE TABLE IF NOT EXISTS contract_items (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id    uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  order_item_id  uuid REFERENCES sales_order_items(id),
  item_id        uuid NOT NULL REFERENCES catalogue_items(id),
  product        text NOT NULL CHECK (product IN ('connect', 'commai')),
  code           text NOT NULL,
  description    text NOT NULL,
  charge_type    text NOT NULL CHECK (charge_type IN ('recurring', 'usage')),
  quantity       numeric(18, 6) NOT NULL DEFAULT 1,
  unit           text NOT NULL,
  monthly_amount numeric(14, 4) NOT NULL DEFAULT 0,      -- recurring: per month, after discounts
  meter          text,                                   -- usage: the meter rated
  price          jsonb NOT NULL DEFAULT '{}',            -- usage: the agreed price (snapshot)
  discount_pct   numeric(6, 3) NOT NULL DEFAULT 0,
  currency       text NOT NULL,
  site_id        uuid REFERENCES sites(id) ON DELETE SET NULL,
  link_id        uuid REFERENCES links(id) ON DELETE SET NULL,
  -- a credit tied to a site or link stops when that site or link is removed
  follows        text CHECK (follows IN ('site', 'link')),
  starts_on      date NOT NULL,
  term_months    int NOT NULL DEFAULT 1,
  ends_on        date,                                   -- exclusive; NULL: until ended
  status         text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'ended')),
  created_by     text NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now(),
  ended_by       text,
  ended_at       timestamptz,
  CHECK (ends_on IS NULL OR ends_on > starts_on),
  CHECK ((charge_type = 'usage') = (meter IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS contract_items_customer ON contract_items (customer_id, product, starts_on);

CREATE TABLE IF NOT EXISTS one_off_charges (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id    uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  order_item_id  uuid REFERENCES sales_order_items(id),
  product        text NOT NULL CHECK (product IN ('connect', 'commai')),
  code           text NOT NULL,
  description    text NOT NULL,
  quantity       numeric(18, 6) NOT NULL,
  unit           text NOT NULL,
  amount         numeric(14, 4) NOT NULL,
  currency       text NOT NULL,
  charge_on      date NOT NULL,                          -- billed on that month's invoice
  created_by     text NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS one_off_charges_customer ON one_off_charges (customer_id, product, charge_on);

-- Usage for catalogue usage items (TMF635-style). Each event counts once: the
-- sender's reference is unique per organisation and meter.
CREATE TABLE IF NOT EXISTS usage_events (
  id           bigserial PRIMARY KEY,
  customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  meter        text NOT NULL CHECK (meter ~ '^[a-z0-9][a-z0-9._:-]{0,63}$'),
  quantity     numeric(18, 6) NOT NULL CHECK (quantity >= 0),
  at           timestamptz NOT NULL,
  ref          text NOT NULL CHECK (length(ref) BETWEEN 1 AND 120),
  source       text NOT NULL DEFAULT 'api',
  detail       jsonb NOT NULL DEFAULT '{}',
  created_by   text NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (customer_id, meter, ref)
);
CREATE INDEX IF NOT EXISTS usage_events_customer ON usage_events (customer_id, meter, at);

-- Plan invoices also carry what was bought through quotes.
ALTER TABLE billing_invoice_lines DROP CONSTRAINT IF EXISTS billing_invoice_lines_kind_check;
ALTER TABLE billing_invoice_lines ADD CONSTRAINT billing_invoice_lines_kind_check
  CHECK (kind IN ('plan', 'site', 'commit', 'burst', 'satellite', 'circuit', 'credit', 'usage', 'voice',
                  'recurring', 'one_off', 'rated'));

-- Example catalogue until ExaCarib agrees prices: every item and price is
-- marked example and shown as "Example data".
INSERT INTO catalogue_items (code, name, description, product, family, charge_type, unit, price_source, fulfilment,
                             plan_id, max_discount_pct, example, position, created_by, meter)
SELECT v.code, v.name, v.description, v.product, v.family, v.charge_type, v.unit, v.price_source, v.fulfilment,
       CASE WHEN v.fulfilment = 'subscription'
            THEN (SELECT id FROM plans p WHERE p.product = v.product AND p.name = v.plan_name) END,
       v.max_discount_pct, true, v.position, 'system:schema',
       CASE WHEN v.charge_type = 'usage' THEN replace(split_part(v.code, '.', 2), '-', '_') END
FROM (VALUES
  ('connect.plan', 'Connect Standard plan', 'The Connect plan: portal, AI routing, Storm Mode and metering.',
   'connect', 'plan', 'recurring', 'month', 'plan_fee', 'subscription', 'Connect Standard', 10::numeric, 10),
  ('connect.site', 'Connect site', 'One site on the ExaCarib network, billed per month on the plan.',
   'connect', 'site', 'recurring', 'site', 'site_monthly', 'site', NULL, 15, 20),
  ('connect.link', 'Carrier link commit', 'Committed bandwidth on one carrier link, per Mbps per month.',
   'connect', 'link', 'recurring', 'Mbps', 'commit_per_mbps', 'link', NULL, 15, 30),
  ('connect.box', 'ExaCarib box', 'Edge box, shipped ready to install (one-off).',
   'connect', 'hardware', 'one_off', 'box', 'catalogue', 'manual', NULL, 10, 40),
  ('connect.install', 'Site installation', 'An engineer installs and tests the box on site.',
   'connect', 'service', 'one_off', 'site', 'catalogue', 'manual', NULL, 25, 50),
  ('connect.managed', 'Managed network service', 'ExaCarib watches and runs the network for you, per site.',
   'connect', 'support', 'recurring', 'site', 'catalogue', 'immediate', NULL, 15, 60),
  ('connect.engineer-hours', 'Engineer time', 'Changes and visits on request, by the hour; 2 hours a month included.',
   'connect', 'usage', 'usage', 'hour', 'catalogue', 'immediate', NULL, 10, 70),
  ('commai.plan', 'Jibsy Standard plan', 'The Jibsy plan: inbox, channels, AI agents and voice.',
   'commai', 'plan', 'recurring', 'month', 'plan_fee', 'subscription', 'Jibsy Standard', 10, 110),
  ('commai.seat', 'Jibsy seat', 'One person using Jibsy, per month. Cheaper per seat in bigger bands.',
   'commai', 'seat', 'recurring', 'seat', 'catalogue', 'immediate', NULL, 20, 120),
  ('commai.onboarding', 'Jibsy onboarding', 'Set-up, channel connection and training (one-off).',
   'commai', 'service', 'one_off', 'each', 'catalogue', 'manual', NULL, 50, 130)
) AS v(code, name, description, product, family, charge_type, unit, price_source, fulfilment, plan_name,
       max_discount_pct, position)
ON CONFLICT (code) DO NOTHING;

INSERT INTO catalogue_prices (item_id, version, currency, effective_from, model, unit_price, tiers, included_qty,
                              minimum, term_discounts, example, created_by)
SELECT i.id, 1, 'USD', DATE '2026-01-01', v.model, v.unit_price, v.tiers::jsonb, v.included_qty, 0,
       v.term_discounts::jsonb, true, 'system:schema'
FROM (VALUES
  ('connect.box', 'per_unit', 450::numeric, '[]', 0::numeric, '[]'),
  ('connect.install', 'per_unit', 300, '[]', 0, '[]'),
  ('connect.managed', 'per_unit', 120, '[]', 0, '[{"min_months": 12, "pct": 5}, {"min_months": 36, "pct": 10}]'),
  ('connect.engineer-hours', 'per_unit', 85, '[]', 2, '[]'),
  ('commai.seat', 'volume', 0,
   '[{"up_to": 10, "unit_price": "25"}, {"up_to": 50, "unit_price": "20"}, {"up_to": null, "unit_price": "15"}]',
   0, '[{"min_months": 12, "pct": 5}, {"min_months": 24, "pct": 10}]'),
  ('commai.onboarding', 'flat', 500, '[]', 0, '[]')
) AS v(code, model, unit_price, tiers, included_qty, term_discounts)
JOIN catalogue_items i ON i.code = v.code
WHERE NOT EXISTS (SELECT 1 FROM catalogue_prices p WHERE p.item_id = i.id);
