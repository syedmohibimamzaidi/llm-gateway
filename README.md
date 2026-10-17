# LLM Gateway

## Overview and problem solved

Applications that call multiple LLM providers need to handle different request
formats, credentials, failures, and usage reports. LLM Gateway puts those
concerns behind one authenticated chat API for OpenAI and Anthropic.

Clients select a model alias such as `fast` or `smart`. The gateway routes the
request, limits usage, optionally reuses a cached response, and records token,
latency, and estimated cost data for a React dashboard. This is a local MVP,
with an emphasis on bounded failure handling and visibility into each request.

## Tech stack

- **Backend:** Python 3.11+, FastAPI, Pydantic, HTTPX, and Uvicorn.
- **Storage:** Redis for rate counters/cache; PostgreSQL with Psycopg for telemetry
  and managed-key records. Schema changes use transactional SQL migrations.
- **Frontend:** React 19 and Vite 8, with authenticated analytics through a proxy.
- **Tooling:** Docker Compose, pytest, and Node's built-in test runner.

## Main features

- `POST /v1/chat` normalizes provider requests and responses, with configurable
  aliases, usage reporting, request IDs, and attempt metadata.
- Bounded retries and provider fallback handle transient failures; an explicit
  provider override keeps a request on the chosen provider.
- Per-key rate limits and opt-in caching control repeated provider work.
- PostgreSQL telemetry powers request counts, success/cache-hit rates, tokens,
  estimated USD cost, mean/p95 latency, and provider breakdowns.
- The dashboard supports 1-hour, 24-hour, 7-day, and 30-day windows, with 25-row
  request-history pages. Changing the window resets pagination.
- Protected `/api-keys` endpoints create, list, and revoke managed gateway keys.

## Architecture

```mermaid
flowchart LR
    Client[Chat client] -->|Bearer gateway key| API[FastAPI gateway]
    API --> Redis[(Redis: rate limits and cache)]
    API --> Router[Retry and fallback router]
    Router --> OpenAI[OpenAI adapter]
    Router --> Anthropic[Anthropic adapter]
    API --> Postgres[(PostgreSQL: telemetry and key hashes)]
    Dashboard[React dashboard] -->|Authenticated analytics| API
```

A chat request passes through authentication, body validation, rate limiting,
cache lookup, and provider routing. A cache hit skips the provider. The telemetry
middleware records the outcome, including rejected and failed chat requests.
The dashboard reads `/analytics/summary`, `/analytics/providers`, and `/requests`;
each endpoint scopes results to the authenticated key.

## Key design decisions

### Separate gateway access from provider credentials

`GATEWAY_API_KEY` authenticates gateway clients and acts as the management key.
`OPENAI_API_KEY` and `ANTHROPIC_API_KEY` authenticate the adapters upstream.
Managed keys can use chat and their own analytics, but cannot manage other keys.
New managed secrets are returned once; PostgreSQL stores SHA-256 hashes.
Revocation takes effect at the next authentication check. The dashboard keeps
its key in page memory rather than a URL or browser storage.

### Retry only failures that may recover

Only provider timeouts, HTTP 429, and HTTP 5xx trigger retries. Authentication,
malformed-request, and other nontransient errors stop without fallback. The
default is two additional attempts per provider with exponential backoff;
fallback requires the second provider's model mapping and credential.
An explicit `provider` disables fallback while allowing retries there.

A default **45-second routing deadline** covers all provider calls and backoff.
Expiry cancels the in-flight call and prevents further attempts. Initial/final
providers, retry count, safe failure reasons, and attempt paths make recovery
visible. The deadline does not cover the entire HTTP lifecycle.

### Apply rate limits before caching

Redis atomically counts requests per key in a fixed window: the default is
60 requests per 60 seconds, including cache hits. Exceeding it returns 429.
Redis failure at this step returns 503 before any provider call; a later
cache-only failure lets an allowed request proceed without caching.

Caching requires `cache: true`, explicit `temperature: 0`, and a final user
message. Only success without retries or fallback is stored, for 60 seconds by
default. The fingerprint includes the gateway key identifier, selected provider,
configured model ID, messages, and generation parameters. A hit makes no provider
call and receives a new request ID. Chat repeats cached usage for compatibility;
telemetry records zero new provider tokens and cost. Redis cache values contain
response text until expiry, although key names contain only a hash.

### Keep telemetry useful without retaining conversations

PostgreSQL records request IDs, key identifiers, model/provider routing, tokens,
latency, outcome, attempts, cache hits, price version, and estimated cost. It
stores no raw messages, response text, API keys, or authorization headers.
Analytics also excludes key hashes. Valid-key malformed requests retain their
key scope because authentication runs before body validation.

Telemetry writes have a 750 ms budget. `X-Telemetry-Status` reports `stored`,
`failed`, or `disabled`; failures log the request ID and exception type while
preserving the chat result. This trades guaranteed accounting for availability:
there is no durable replay, so dashboard totals can be incomplete.

### Make pricing explicit and versioned

Costs use the final provider's reported usage and **returned model ID**, with
USD-per-million-token rates and decimal arithmetic. Compose selects
`prices.live.json`, a bundled rate snapshot with version
`official-standard-text-2026-10-03`; recheck rates before billing decisions.
`prices.json` and `prices.example.json` are **test pricing only**. Missing rates
yield null cost and an unpriced-success count, not free usage. Invalid configured
prices fail startup. Stored estimates are not recalculated when rates change.

## Testing and verified results

Tests cover authentication/revocation, malformed input, retry/fallback paths,
strict overrides, deadlines, cache isolation/expiry, rate limiting, private
telemetry, known-row analytics, and dependency failures. Provider responses in
the automated suite are fake; no paid calls are needed.

Checks without service URLs pass **64 backend tests**, with **5 integration
tests skipped**; **3 frontend tests** also pass. Recorded full-service verification
passed **69 backend tests** with real Redis/PostgreSQL, and the dashboard
production build succeeded. The full-service result is recorded evidence; it
does not establish current provider availability or production readiness.

The fake-provider load script sends 40 requests at peak concurrency 5: ten cache
warm-ups, ten repeats, sixteen unique requests, and four fallback cases. A
recorded run verified all 40 telemetry rows, zero HTTP errors, ten cache hits,
and four successful fallbacks. It measured 0.273 seconds overall and 23.855 ms
p95 response latency, with USD 0.000510 in **fixture-priced** estimated usage.
These are **in-process ASGI measurements with simulated providers**, not
live-provider latency, billed cost, or production HTTP throughput.

To run checks from the repository root, use Python 3.11+ (commands use 3.12),
Node.js 22.12+, and npm:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m pytest -q
(cd dashboard && npm ci && npm test && npm run build)
```

Service tests skip unless test URLs are supplied. With Compose running and its
migrations complete, run the full backend suite against disposable local data:

```sh
export TEST_DATABASE_URL=postgresql://gateway:gateway-dev-password@127.0.0.1:5434/gateway
export TEST_REDIS_URL=redis://127.0.0.1:6380/0
.venv/bin/python -m pytest -q
.venv/bin/python scripts/load_test.py
```

Integration tests and the load script create random fixture keys/rows and remove
their own data. Frontend helper tests/builds do not verify interactive browser
behavior. Live-provider availability, billing, and deployment need separate checks.

## Known limitations

- Local development stack only: the dashboard uses Vite's development server;
  there is no cloud deployment or production security configuration.
- Text-only, nonstreaming chat; no separate administrator roles, key-rotation API,
  or key-creation audit log. The configured management key rotates via config.
- Best-effort telemetry has no durable queue/replay. Database outages make
  analytics, management, and managed-key authentication unavailable (503).
- `/health` checks liveness only. Authentication, Redis, and telemetry have
  separate time bounds outside the provider-routing deadline.
- Cost estimates exclude provider prompt-cache billing adjustments, taxes, and
  unreported usage from failed attempts. Null estimates are omitted from totals.
- Cache repeatability is a policy, not a guarantee of deterministic model output.
  Offset pagination can shift if new requests arrive between page reads.

## Author

Mohib Zaidi
