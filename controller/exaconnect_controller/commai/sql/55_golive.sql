-- Go-live registry (ADR 0028): every country, channel, language, carrier and
-- region starts off, and an ExaCarib admin switches it on only after its
-- written operating and security criteria are recorded as met.

CREATE TABLE IF NOT EXISTS commai_capabilities (
    kind        text NOT NULL,          -- country | channel | language | carrier | region | feature
    key         text NOT NULL,          -- e.g. 'TT', 'telegram', 'es', 'sim-carrier-1', 'eu-west'
    name        text NOT NULL DEFAULT '',
    status      text NOT NULL DEFAULT 'off' CHECK (status IN ('off', 'pilot', 'on')),
    details     jsonb NOT NULL DEFAULT '{}'::jsonb,   -- owner-defined facts (capability matrix, limits)
    updated_by  text NOT NULL DEFAULT '',
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (kind, key)
);

CREATE TABLE IF NOT EXISTS commai_capability_criteria (
    kind        text NOT NULL,
    key         text NOT NULL,
    criterion   text NOT NULL,          -- stable id, e.g. 'security-review'
    text        text NOT NULL,          -- the written criterion
    met         boolean NOT NULL DEFAULT false,
    evidence    text NOT NULL DEFAULT '',
    checked_by  text NOT NULL DEFAULT '',
    checked_at  timestamptz,
    PRIMARY KEY (kind, key, criterion),
    FOREIGN KEY (kind, key) REFERENCES commai_capabilities (kind, key) ON DELETE CASCADE
);

-- A pilot is on for the named customers only.
CREATE TABLE IF NOT EXISTS commai_capability_pilots (
    kind        text NOT NULL,
    key         text NOT NULL,
    customer_id uuid NOT NULL REFERENCES customers (id) ON DELETE CASCADE,
    PRIMARY KEY (kind, key, customer_id),
    FOREIGN KEY (kind, key) REFERENCES commai_capabilities (kind, key) ON DELETE CASCADE
);
