-- Each organisation's own SIP trunks, and a primary and secondary trunk per
-- location (ADR 0044). Applied after organisation.sql (it uses org_locations),
-- idempotently, at startup.

-- A trunk is a voice_carriers row with a customer_id; ExaCarib's own carriers
-- have none. The organisation's trunks need no go-live switch (their contract is
-- the organisation's own) and carry no ExaCarib rate sheet.
ALTER TABLE voice_carriers ADD COLUMN IF NOT EXISTS customer_id uuid REFERENCES customers(id) ON DELETE CASCADE;
CREATE INDEX IF NOT EXISTS voice_carriers_customer ON voice_carriers (customer_id) WHERE customer_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS voice_carriers_customer_name ON voice_carriers (customer_id, lower(name))
  WHERE customer_id IS NOT NULL;

-- The organisation's default trunk, and whether ExaCarib's carriers take calls
-- when none of its own trunks can.
CREATE TABLE IF NOT EXISTS voice_trunk_settings (
  customer_id        uuid PRIMARY KEY REFERENCES customers(id) ON DELETE CASCADE,
  default_trunk_id   uuid REFERENCES voice_carriers(id) ON DELETE SET NULL,
  exacarib_fallback  boolean NOT NULL DEFAULT true,
  updated_by         text NOT NULL DEFAULT '',
  updated_at         timestamptz NOT NULL DEFAULT now()
);

-- A primary and a secondary trunk for calls made from a location.
CREATE TABLE IF NOT EXISTS voice_location_trunks (
  org_location_id     uuid PRIMARY KEY REFERENCES org_locations(id) ON DELETE CASCADE,
  customer_id         uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  primary_trunk_id    uuid REFERENCES voice_carriers(id) ON DELETE SET NULL,
  secondary_trunk_id  uuid REFERENCES voice_carriers(id) ON DELETE SET NULL,
  updated_by          text NOT NULL DEFAULT '',
  updated_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS voice_location_trunks_customer ON voice_location_trunks (customer_id);

-- Numbers that arrive on the organisation's own trunk (its carrier owns them).
ALTER TABLE voice_numbers DROP CONSTRAINT IF EXISTS voice_numbers_source_check;
ALTER TABLE voice_numbers ADD CONSTRAINT voice_numbers_source_check CHECK (source IN ('new', 'ported', 'trunk'));

-- Each carrier and trunk keeps its Kamailio dispatcher set id for good, so adding,
-- removing or switching off one never renumbers the others between the
-- controller's answer and Kamailio's loaded list. Existing carriers keep the
-- numbers they had (their position among the enabled carriers by key).
ALTER TABLE voice_carriers ADD COLUMN IF NOT EXISTS set_id int;
UPDATE voice_carriers c SET set_id = n.pos + coalesce((SELECT max(set_id) FROM voice_carriers), 0)
FROM (SELECT id, row_number() OVER (ORDER BY NOT enabled, key) AS pos FROM voice_carriers WHERE set_id IS NULL) n
WHERE c.id = n.id;
CREATE SEQUENCE IF NOT EXISTS voice_carrier_set_ids OWNED BY voice_carriers.set_id;
SELECT setval('voice_carrier_set_ids', greatest(coalesce((SELECT max(set_id) FROM voice_carriers), 0), 1),
              (SELECT max(set_id) FROM voice_carriers) IS NOT NULL);
ALTER TABLE voice_carriers ALTER COLUMN set_id SET DEFAULT nextval('voice_carrier_set_ids');
CREATE UNIQUE INDEX IF NOT EXISTS voice_carriers_set_id ON voice_carriers (set_id);
