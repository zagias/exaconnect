-- Phone audio jobs are capabilities for one real SIP participant, never portal
-- credentials. Operational evidence expires; configuration cannot enable AI.
CREATE TABLE IF NOT EXISTS voice_ai_phone_sessions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  token_hash text NOT NULL,
  worker_id text NOT NULL,
  provider_fingerprint text NOT NULL,
  room_name text NOT NULL,
  participant_identity text NOT NULL,
  sip_call_id text NOT NULL,
  transfer_target text NOT NULL,
  acceptance boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  heartbeat_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL DEFAULT now() + interval '30 minutes',
  ended_at timestamptz,
  end_reason text NOT NULL DEFAULT '',
  cleanup_pending boolean NOT NULL DEFAULT true,
  transfer_attempt_id uuid,
  transfer_lease_until timestamptz,
  handover_state text NOT NULL DEFAULT 'none'
    CHECK (handover_state IN ('none', 'requested', 'accepted', 'failed')),
  UNIQUE (conversation_id),
  UNIQUE (room_name, participant_identity, sip_call_id)
);
CREATE TABLE IF NOT EXISTS voice_ai_phone_turns (
  session_id uuid NOT NULL REFERENCES voice_ai_phone_sessions(id) ON DELETE CASCADE,
  request_id uuid NOT NULL,
  message_id uuid REFERENCES messages(id) ON DELETE SET NULL,
  result jsonb,
  PRIMARY KEY (session_id, request_id)
);
CREATE TABLE IF NOT EXISTS voice_ai_phone_evidence (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  worker_id text NOT NULL,
  provider_fingerprint text NOT NULL,
  phrase text NOT NULL,
  expires_at timestamptz NOT NULL DEFAULT now() + interval '60 seconds',
  proved_at timestamptz,
  media_proved_at timestamptz,
  room_name text,
  participant_identity text,
  sip_call_id text
);
CREATE TABLE IF NOT EXISTS voice_ai_phone_runtime (
  worker_id text NOT NULL,
  customer_id uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  provider_fingerprint text NOT NULL,
  speech_verified_at timestamptz NOT NULL,
  transfer_verified_at timestamptz NOT NULL,
  transfer_session_id uuid NOT NULL REFERENCES voice_ai_phone_sessions(id) ON DELETE CASCADE,
  lab_only boolean NOT NULL DEFAULT true,
  expires_at timestamptz NOT NULL,
  PRIMARY KEY (worker_id, customer_id)
);
CREATE TABLE IF NOT EXISTS voice_ai_phone_pbx_runtime (
  instance_id text PRIMARY KEY,
  version text NOT NULL,
  version_secure boolean NOT NULL,
  transfer_enabled boolean NOT NULL,
  checked_at timestamptz NOT NULL,
  nonce uuid NOT NULL UNIQUE,
  expires_at timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS voice_ai_phone_pbx_receipts (
  nonce uuid PRIMARY KEY,
  seen_at timestamptz NOT NULL DEFAULT now()
);
