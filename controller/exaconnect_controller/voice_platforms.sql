-- Microsoft Teams, Zoom Phone, Webex Calling, Google Voice and contact-centre platforms joined to Jibsy (ADR 0056).
-- Applied after voice_trunks.sql, idempotently, at startup.

-- A platform link is a voice_carriers row (so Kamailio's dispatcher, allow-list and
-- OPTIONS health checks treat it like a trunk) marked with its platform; it never
-- shows among the organisation's SIP trunks or in their routing.
ALTER TABLE voice_carriers ADD COLUMN IF NOT EXISTS platform text;
-- Trunk names are unique per organisation; a platform link may share a trunk's name.
DROP INDEX IF EXISTS voice_carriers_customer_name;
CREATE UNIQUE INDEX IF NOT EXISTS voice_carriers_trunk_name ON voice_carriers (customer_id, lower(name))
  WHERE customer_id IS NOT NULL AND platform IS NULL;

CREATE TABLE IF NOT EXISTS voice_platform_links (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id   uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  platform      text NOT NULL CHECK (platform IN ('teams', 'zoom', 'webex', 'google', 'genesys', 'nice', 'talkdesk')),
  carrier_id    uuid NOT NULL UNIQUE REFERENCES voice_carriers(id) ON DELETE CASCADE,
  -- The SIP address the platform sends calls to, under ExaCarib's SIP domain
  -- (Teams: the organisation's own subdomain it adds to its Microsoft 365 tenant).
  fqdn          text NOT NULL UNIQUE,
  -- Microsoft 365's TXT value for the subdomain, for ExaCarib to publish in DNS.
  dns_txt       text NOT NULL DEFAULT '',
  dns_done      boolean NOT NULL DEFAULT false,
  created_by    text NOT NULL DEFAULT '',
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (customer_id, platform)
);

-- People who make and take calls in the platform's app, with the number Jibsy gives
-- them and, if wanted, an extension Jibsy users can dial.
CREATE TABLE IF NOT EXISTS voice_platform_users (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id    uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  link_id        uuid NOT NULL REFERENCES voice_platform_links(id) ON DELETE CASCADE,
  name           text NOT NULL,
  account        text NOT NULL,          -- sign-in on the platform (Teams UPN, Zoom email)
  number_id      uuid NOT NULL UNIQUE REFERENCES voice_numbers(id),
  extension      text,
  enabled        boolean NOT NULL DEFAULT true,
  created_by     text NOT NULL DEFAULT '',
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (link_id, account)
);
CREATE INDEX IF NOT EXISTS voice_platform_users_customer ON voice_platform_users (customer_id);
CREATE UNIQUE INDEX IF NOT EXISTS voice_platform_users_ext ON voice_platform_users (customer_id, extension)
  WHERE extension IS NOT NULL;

-- A number can ring a platform user.
ALTER TABLE voice_numbers DROP CONSTRAINT IF EXISTS voice_numbers_target_type_check;
ALTER TABLE voice_numbers ADD CONSTRAINT voice_numbers_target_type_check
  CHECK (target_type IN ('none', 'user', 'ring_group', 'queue', 'menu', 'ai', 'platform_user'));

