# LLM Gateway — local MVP

An implementation of the project plan in `LLM_Gateway_MVP.txt`.

## Development

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
```

## API

Copy `.env.example` to `.env` and configure a gateway key, model aliases and provider credentials. Start the API with:

```sh
.venv/bin/uvicorn gateway.main:app --host 127.0.0.1 --port 8000
```

`gateway.main` loads `.env` before app creation and overrides stale exported values. `POST /v1/chat` accepts a model alias, messages, generation parameters and an optional strict provider override. Callers use a bearer gateway key; provider keys are used by the adapters. Tests inject fake providers and never make paid calls.

Transient timeouts, rate limits and server errors receive bounded exponential retries and compatible fallback. Explicit provider overrides stay on that provider. Authentication and invalid-request errors are not retried. The default provider-routing deadline is 45 seconds.

Redis is required for per-key rate limiting. The rate limit runs before cache lookup. Caching is opt-in, requires temperature zero and a final user message, and isolates keys/providers/models/messages/generation options. Redis rate-limit failures return 503; cache-only errors bypass caching. Configure `REDIS_URL`, the rate window/threshold and cache TTL in `.env`.

## Telemetry and pricing

Configure `DATABASE_URL` and apply `migrations/001_request_telemetry.sql` with `psql -v ON_ERROR_STOP=1`. PostgreSQL records request metadata, token usage, route attempts and estimated USD cost without prompts, response bodies or credentials. Each request reports telemetry status in `X-Telemetry-Status`. Writes are best effort with no durable replay queue.

`prices.json` and `prices.example.json` contain numeric test-only rates; `prices.live.json` contains versioned standard text-token rates for the configured live IDs. Configure `GATEWAY_PRICE_CONFIG_PATH` to select a file. Estimates exclude provider prompt-cache adjustments and taxes.

Run real-service checks by setting `TEST_DATABASE_URL` and `TEST_REDIS_URL` before `.venv/bin/python -m pytest -q`. These tests remove only their random fixture rows and Redis keys.

Authenticated `/analytics/summary`, `/analytics/providers` and `/requests` expose only the caller key’s metadata. Choose `window=1h|24h|7d|30d`; history supports `limit` and `offset`, ordered by descending timestamp and request ID. Analytics failures return 503.
