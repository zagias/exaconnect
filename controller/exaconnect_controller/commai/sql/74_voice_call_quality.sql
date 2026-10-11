-- Call quality as each person's browser heard it (browser phone and Jibsy Phone):
-- one row per answered call leg, sent when the call ends. Numbers only, no audio or
-- addresses; kept 90 days. Used to trace a bad call to the network it was on.
CREATE TABLE IF NOT EXISTS voice_call_quality (
  id               bigserial PRIMARY KEY,
  customer_id      uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  user_id          text NOT NULL,
  extension        text NOT NULL,
  call_id          text NOT NULL,
  direction        text NOT NULL CHECK (direction IN ('in', 'out')),
  secs             integer NOT NULL,
  codec            text NOT NULL DEFAULT '',
  rx_packets       integer NOT NULL,
  rx_lost          integer NOT NULL,
  concealed_pct    real NOT NULL,
  bad_stretches    integer NOT NULL,
  jitter_ms        integer NOT NULL,
  jitter_max_ms    integer NOT NULL,
  jitter_buffer_ms integer NOT NULL,
  rtt_ms           integer,
  rtt_max_ms       integer,
  up_lost_pct      real,
  up_jitter_ms     integer,
  path             text NOT NULL DEFAULT '',
  network          text NOT NULL DEFAULT '',
  device           text NOT NULL DEFAULT '',
  created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS voice_call_quality_recent ON voice_call_quality (customer_id, created_at DESC);
CREATE INDEX IF NOT EXISTS voice_call_quality_created ON voice_call_quality (created_at);
