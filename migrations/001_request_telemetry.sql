BEGIN;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS request_telemetry (
    request_id UUID PRIMARY KEY,
    gateway_key_id CHAR(64),
    model_alias TEXT,
    initial_provider TEXT,
    final_provider TEXT,
    actual_model TEXT,
    input_tokens INTEGER CHECK (input_tokens IS NULL OR input_tokens >= 0),
    output_tokens INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0),
    latency_ms DOUBLE PRECISION NOT NULL CHECK (latency_ms >= 0),
    status_code SMALLINT NOT NULL,
    outcome TEXT NOT NULL,
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    failure_reason TEXT,
    cache_hit BOOLEAN NOT NULL DEFAULT FALSE,
    estimated_cost_usd NUMERIC(20, 12) CHECK (estimated_cost_usd IS NULL OR estimated_cost_usd >= 0),
    price_version TEXT NOT NULL,
    attempts JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS request_telemetry_created_at_idx
    ON request_telemetry (created_at DESC);
CREATE INDEX IF NOT EXISTS request_telemetry_gateway_key_created_at_idx
    ON request_telemetry (gateway_key_id, created_at DESC);
CREATE INDEX IF NOT EXISTS request_telemetry_provider_created_at_idx
    ON request_telemetry (final_provider, created_at DESC);

INSERT INTO schema_migrations (version)
VALUES ('001_request_telemetry')
ON CONFLICT (version) DO NOTHING;

COMMIT;
