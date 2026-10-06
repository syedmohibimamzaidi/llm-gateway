BEGIN;

CREATE TABLE IF NOT EXISTS gateway_keys (
    id UUID PRIMARY KEY,
    key_hash CHAR(64) NOT NULL UNIQUE,
    name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 100),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS gateway_keys_created_at_idx
    ON gateway_keys (created_at DESC);

INSERT INTO schema_migrations (version)
VALUES ('002_gateway_keys')
ON CONFLICT (version) DO NOTHING;

COMMIT;
