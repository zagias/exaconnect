-- The PBX's check before each outside call (ADR 0027, voice/pbx.py): one row
-- per call with the answer FreeSWITCH got. Refused calls are also kept as
-- blocked call records (voice_cdrs), as simulated calls are.

CREATE TABLE IF NOT EXISTS voice_pbx_authorisations (
  id            bigserial PRIMARY KEY,
  customer_id   uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  call_id       text NOT NULL,
  voice_user_id uuid,
  to_number     text NOT NULL,
  from_number   text NOT NULL DEFAULT '',
  allowed       boolean NOT NULL,
  code          text NOT NULL DEFAULT '',
  reason        text NOT NULL DEFAULT '',
  route         jsonb NOT NULL DEFAULT '[]',
  sets          jsonb NOT NULL DEFAULT '[]',
  at            timestamptz NOT NULL DEFAULT now(),
  UNIQUE (customer_id, call_id)
);
CREATE INDEX IF NOT EXISTS voice_pbx_authorisations_at ON voice_pbx_authorisations (customer_id, at DESC);
