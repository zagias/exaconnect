-- Jibsy Phone rings when the app is closed (ADR 0045): each device a person turns
-- "Ring when the app is closed" on for keeps a Web Push subscription here. The
-- endpoint and keys come from the browser; they let ExaCarib send that one
-- device an encrypted message and nothing else. Applied idempotently at startup.
CREATE TABLE IF NOT EXISTS voice_push_subscriptions (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id    uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  voice_user_id  uuid NOT NULL REFERENCES voice_users(id) ON DELETE CASCADE,
  user_id        uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  endpoint       text NOT NULL UNIQUE,
  p256dh         text NOT NULL,
  auth           text NOT NULL,
  device         text NOT NULL DEFAULT '',
  created_at     timestamptz NOT NULL DEFAULT now(),
  last_sent_at   timestamptz,
  last_error     text NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS voice_push_subscriptions_user ON voice_push_subscriptions (voice_user_id);
