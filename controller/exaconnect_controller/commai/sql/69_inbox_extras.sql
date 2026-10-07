-- Inbox, channel and API gap fixes for phases 0 to 2 (ADR 0032).
-- Applied after the other CommAI files, idempotently, at startup.

-- Routing by intent and skills. The AI writes the intent it judged; a rule may
-- match on it, and a rule may require skills of the person (and team) it picks.
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS intent text NOT NULL DEFAULT '';
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS required_skills text[] NOT NULL DEFAULT '{}';
ALTER TABLE commai_routing_rules ADD COLUMN IF NOT EXISTS skills text[] NOT NULL DEFAULT '{}';

-- Service-target reminders and escalations sent for a conversation, so each
-- one goes out once ("first_reply:soon", "first_reply:missed", "resolve:missed"...).
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS target_alerts text[] NOT NULL DEFAULT '{}';

-- Typing and presence: short-lived, overwritten in place, never an event a
-- webhook sees. who_kind is 'user' (staff) or 'contact' (the customer).
CREATE TABLE IF NOT EXISTS commai_presence (
  customer_id      uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  conversation_id  uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  who_kind         text NOT NULL CHECK (who_kind IN ('user', 'contact')),
  who              text NOT NULL,
  name             text NOT NULL DEFAULT '',
  typing_until     timestamptz,
  seen_at          timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (conversation_id, who_kind, who)
);
CREATE INDEX IF NOT EXISTS commai_presence_customer ON commai_presence (customer_id, seen_at DESC);

-- Rate limits shared by every API process: one counter per caller per minute.
CREATE TABLE IF NOT EXISTS api_rate_counters (
  bucket        text NOT NULL,
  window_start  timestamptz NOT NULL,
  hits          int NOT NULL DEFAULT 0,
  PRIMARY KEY (bucket, window_start)
);
-- A key may have its own budget (requests a minute); NULL uses the default.
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS rate_per_min int;

-- Visitor AI calls from the website chat: which widget key and visitor started it.
ALTER TABLE ai_calls ADD COLUMN IF NOT EXISTS widget_key_id uuid;
ALTER TABLE ai_calls ADD COLUMN IF NOT EXISTS visitor text NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS ai_calls_visitor ON ai_calls (widget_key_id, started_at) WHERE widget_key_id IS NOT NULL;

-- Files staff attach to replies use channel_files too (owner 'staff:<email>').
CREATE INDEX IF NOT EXISTS channel_files_owner ON channel_files (customer_id, owner) WHERE conversation_id IS NULL;
