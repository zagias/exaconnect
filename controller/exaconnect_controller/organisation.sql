-- One organisation, set up in one place (ADR 0043). Applied after the Jibsy
-- and integrations files, idempotently, at startup.

-- Company details, shown and edited under Organisation > Company details.
ALTER TABLE customers ADD COLUMN IF NOT EXISTS country text NOT NULL DEFAULT '';      -- ISO 3166-1 alpha-2
ALTER TABLE customers ADD COLUMN IF NOT EXISTS timezone text NOT NULL DEFAULT '';
ALTER TABLE customers ADD COLUMN IF NOT EXISTS address text NOT NULL DEFAULT '';
ALTER TABLE customers ADD COLUMN IF NOT EXISTS phone text NOT NULL DEFAULT '';
ALTER TABLE customers ADD COLUMN IF NOT EXISTS website text NOT NULL DEFAULT '';
-- When an owner or ExaCarib marked the set-up checklist as finished early.
ALTER TABLE customers ADD COLUMN IF NOT EXISTS setup_done_at timestamptz;

-- The organisation's locations: each branch or office entered once. Connect
-- sites, phone sites (emergency addresses) and Jibsy locations (opening hours)
-- point at the location they stand for.
CREATE TABLE IF NOT EXISTS org_locations (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id    uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  name           text NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
  address_line1  text NOT NULL DEFAULT '',
  address_line2  text NOT NULL DEFAULT '',
  city           text NOT NULL DEFAULT '',
  island         text NOT NULL DEFAULT '',
  country        text NOT NULL DEFAULT '',          -- ISO 3166-1 alpha-2
  postcode       text NOT NULL DEFAULT '',
  timezone       text NOT NULL DEFAULT 'America/Port_of_Spain',
  latitude       double precision,
  longitude      double precision,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS org_locations_name ON org_locations (customer_id, lower(name));

ALTER TABLE sites ADD COLUMN IF NOT EXISTS org_location_id uuid REFERENCES org_locations(id) ON DELETE SET NULL;
ALTER TABLE voice_sites ADD COLUMN IF NOT EXISTS org_location_id uuid REFERENCES org_locations(id) ON DELETE SET NULL;
ALTER TABLE commai_locations ADD COLUMN IF NOT EXISTS org_location_id uuid REFERENCES org_locations(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS sites_org_location ON sites (org_location_id);
CREATE INDEX IF NOT EXISTS voice_sites_org_location ON voice_sites (org_location_id);
CREATE INDEX IF NOT EXISTS commai_locations_org_location ON commai_locations (org_location_id);
