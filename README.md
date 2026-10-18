# LLM Gateway

## Overview and problem solved

Applications that call multiple LLM providers need to handle different request
formats, credentials, failures, and usage reports. LLM Gateway puts those
concerns behind one authenticated chat API for OpenAI and Anthropic.

Clients select a model alias such as `fast` or `smart`. The gateway routes the
request, limits usage, optionally reuses a cached response, and records token,
latency, and estimated cost data for a React dashboard. The hosted MVP runs on
Render with Neon PostgreSQL and Upstash Redis.

[Hosted dashboard — gateway key required; access available on request.](https://llm-gateway-private-demo.onrender.com)

Credentials are not publicly shared or embedded in the frontend. Ask the author
for a private gateway key; no provider credentials are needed to view analytics.
The free Render service sleeps after 15 idle minutes, so the first visit may
take about a minute to load. Database wake-up can add a short delay.

## Tech stack

- **Backend:** Python 3.11+, FastAPI, Pydantic, HTTPX, and Uvicorn.
- **Storage:** Redis for rate counters/cache; PostgreSQL with Psycopg for telemetry
  and managed-key records. Schema changes use transactional SQL migrations.
- **Frontend:** React 19 and Vite 8; production assets served by FastAPI on Render
  or Nginx in local Compose, with API requests on the same origin.
- **Tooling:** Docker Compose, pytest, Node's test runner, and GitHub Actions CI.
  Runtime Python dependencies are constrained in `requirements.lock`.

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
The production API image serves the compiled dashboard and API together; no
Vite development proxy is needed. It honors Render's `PORT` on `0.0.0.0`.
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

### Bound work before accepting provider calls

An outer ingress guard applies global/transport-peer throttling before auth,
body parsing, or PostgreSQL writes. Bodies are capped at **64 KiB** with a
5-second receive deadline; messages are capped at **64**, **16,000 characters
per message**, and **32,000 total characters**. Output is capped at **1,024 tokens**
by default. The limits can be lowered with the `GATEWAY_MAX_*` settings in
`.env.example`; hard validation ceilings also prevent arbitrary increases.
These are byte/character bounds, not an exact provider input-token tokenizer.

Production admission uses Redis atomic counters and expiring leases shared
across processes and rolling deploys. Defaults allow eight provider requests
in flight globally and two per key. Render lowers these to **four globally,
one per key, and 256 output tokens**. An omitted output limit uses the lower
server cap; explicit excess is rejected. Saturation returns 429; admission outages
return 503. Leases release on completion, failure, timeout, or cancellation,
and expire after the routing deadline plus ten seconds if a worker disappears.
Shorter new leases cannot shorten older leases during a rolling deploy.

Early throttling defaults to 300 API requests per minute globally and 120 per
transport peer. Forwarded IP headers are deliberately not trusted: clients
cannot rotate buckets by spoofing them. Render proxy peers may share a bucket;
this conservative policy is for a small private demo, not large public traffic.
Early 408/413/429/431 responses do not create telemetry rows or telemetry headers.
Development/test admission uses process-local memory; production requires Redis.

### Apply per-key rate limits before caching

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
USD-per-million-token rates and decimal arithmetic. The hosted profile selects
`prices.live.json`, a bundled rate snapshot with version
`official-standard-text-2026-10-03`; recheck rates before billing decisions.
`prices.json` and `prices.example.json` are **test pricing only**. Missing rates
yield null cost and an unpriced-success count, not free usage. Invalid configured
prices fail startup. Production additionally rejects incomplete/fixture pricing,
weak/template keys, missing static assets, and invalid datastore configuration.
Stored estimates are not recalculated when rates change.

### Keep paid access private and explicitly enabled

Production defaults `GATEWAY_PAID_PROVIDERS_ENABLED=false`. Until the owner
explicitly enables it, chat returns 503 without calling either provider, while
authenticated analytics and key management remain available. There is no public
signup, shared demo key, or credential embedded in the frontend. Give invited
users individual managed keys; keep the management key owner-only. All active
managed keys currently allow both chat and their own analytics.

This is **not a hard dollar budget**. Configure dedicated provider projects and
provider-side limits/alerts before enabling paid calls. Best-effort PostgreSQL
telemetry is for observation, never the authority for spending admission. A
provider call cancelled locally may still incur upstream usage.

## Testing and verified results

Tests cover authentication/revocation, malformed input, retry/fallback paths,
strict overrides, deadlines, cache isolation/expiry, rate limiting, private
telemetry, known-row analytics, and dependency failures. Provider responses in
the automated suite are fake; no paid calls are needed.

The deployment regression suite passes **119 backend tests with real local
Redis/PostgreSQL**, including shared admission/lease expiry, bounded requests,
production validation, same-origin routes, migration replay/rollback, and schema
readiness. All **3 frontend key-input tests** and the dashboard production build
also passed. [GitHub Actions](https://github.com/syedmohibimamzaidi/llm-gateway/actions/runs/39093657491)
passed migrations, the full suite, frontend tests/build/audit, and Docker image
smoke checks with real dependency services. Automated provider responses are
simulated; hosted live-provider checks are recorded separately below.

### Hosted smoke checks

An unpaid hosted check verified invalid-key rejection (401), authenticated
analytics and history (200), and a blocked chat request (503) while paid access
was disabled. The blocked request appeared in history with no routed provider,
tokens, or estimated cost.

The owner then enabled provider access and confirmed the deployed retry limit
was zero. Exactly **two authorized live requests** used the `fast` alias, a
"Say hello" prompt, `max_tokens=24`, and `cache=false`, with one strict override
per provider. Both returned 200 with a greeting, one upstream attempt, zero
retries, no fallback, and `X-Telemetry-Status: stored`.

| Provider | Returned model | Input tokens | Output tokens | Estimated USD |
| --- | --- | ---: | ---: | ---: |
| OpenAI | `gpt-4.1-mini-2025-04-14` | 9 | 9 | $0.000018 |
| Anthropic | `claude-haiku-4-5-20251001` | 9 | 16 | $0.000089 |

Costs were independently calculated from reported usage at the configured
input/output rates: OpenAI **$0.40/$1.60**, Anthropic **$1.00/$5.00**, per million
tokens, using pricing version `official-standard-text-2026-10-03`. Matching
request-history rows, dashboard analytics totals, and provider breakdowns were
verified. The two requests added **18 input tokens, 25 output tokens, and
$0.000107 estimated cost**. These are point-in-time functional checks, not a
live-provider performance benchmark or invoice reconciliation.

### Simulated workload

The fake-provider load script sends 40 requests at peak concurrency 5: ten cache
warm-ups, ten repeats, sixteen unique requests, and four fallback cases. A
recorded run verified all 40 telemetry rows, zero HTTP errors, ten cache hits,
and four successful fallbacks. It measured 0.273 seconds overall and 23.855 ms
p95 response latency, with USD 0.000510 in **fixture-priced** estimated usage.
These are **in-process ASGI measurements with simulated providers**, not
live-provider latency, billed cost, or production HTTP throughput.

Integration tests and the load script create random fixture keys/rows and remove
their own data. Frontend helper tests/builds do not verify interactive browser
behavior. Hosted smoke checks verified the dashboard's authenticated API data;
a full interactive browser review remains separate.

## Known limitations

- Free hosting has cold starts and quota/inactivity limits. Two successful live
  requests do not establish production capacity, uptime, or future availability.
- Hosted backup, restore, and rollback drills remain unverified. Datastore TLS
  settings, eviction/persistence policy, and idle compute usage have not been
  independently audited in the hosting accounts.
- Text-only, nonstreaming chat; no separate administrator roles, key-rotation API,
  or key-creation audit log. The configured management key rotates via config.
- Best-effort telemetry has no durable queue/replay. Database outages make
  analytics, management, and managed-key authentication unavailable (503).
- `/health` checks liveness only. `/readyz` checks Redis and the current database
  schema with a two-second budget and one-second cache; it never calls providers.
  Authentication, ingress, and telemetry have separate bounds outside routing.
- Cost estimates exclude provider prompt-cache billing adjustments, taxes, and
  unreported usage from failed attempts. Null estimates are omitted from totals.
- Cache repeatability is a policy, not a guarantee of deterministic model output.
  Offset pagination can shift if new requests arrive between page reads.

## Hosted deployment and operations

The dashboard and API share one Render Free Docker web service, with Neon Free
for PostgreSQL and Upstash Free for Redis. The checked-in [`render.yaml`](render.yaml)
keeps auto-deploys off and defaults paid-provider access to disabled. Provider
access was explicitly enabled by the owner for the recorded live smoke tests;
the Blueprint defaults are not a report of the current service environment.
Datastores and private environment values are managed separately from this file.

Render polls `/health`, a liveness endpoint that makes **no Redis or PostgreSQL
calls**. `/readyz` remains a separate, on-demand dependency/schema check; do not
use it for frequent uptime polling on this profile. It can wake Neon compute.
The application pool starts with zero connections and has no background SQL
keepalive. Actual analytics, authentication, or telemetry work can wake Neon.

With `GATEWAY_RUN_MIGRATIONS=true`, the container entrypoint validates production
connection security and runs migrations **before opening the HTTP port**. A
migration failure exits with a safe error type; the server does not start. Each
restart safely replays the transactional SQL under an advisory lock and statement
timeout. Use Neon's **direct endpoint**, not its `-pooler` endpoint: migrations
need session-level locking. The direct endpoint also serves the small runtime
pool. This profile uses one database role for migration and runtime; separate
roles remain an improvement before broader use.

Required private Render variables are `GATEWAY_API_KEY` (32–256 characters with
sufficient variety), `DATABASE_URL`, and `REDIS_URL`. The Blueprint supplies
production mode, `GATEWAY_DATASTORE_NETWORK=tls`, migration startup, model aliases,
versioned pricing, and conservative limits. Production ignores dotenv files.
Keep paid access disabled outside owner-authorized use. Enabling it requires
private `OPENAI_API_KEY` and `ANTHROPIC_API_KEY` credentials; these are never
supplied to dashboard visitors.

For Neon, use a direct `postgresql://` connection with `sslmode=verify-full`.
The Blueprint sets `PGSSLROOTCERT=/etc/ssl/certs/ca-certificates.crt`, the system CA
bundle in the Debian-based runtime image. For Upstash, use the **native Redis TCP
endpoint** with `rediss://`, its database password, and certificate/hostname
verification (`ssl_cert_reqs=required&ssl_check_hostname=true`). An HTTPS REST
URL/token does not work with our Redis client. Never disable TLS verification.
Geographically close datastore regions help fit the existing short connection
timeouts. Hosted application connectivity passed the smoke checks; certificate
settings and cold-start timing were not independently audited.

In the Upstash console, keep **Eviction disabled**, persistence enabled, and
automatic paid upgrades disabled. `render.yaml` cannot control another account's
database settings. Use a dedicated database: eviction or state loss would erase
rate counters and concurrency leases as well as cached responses. Redis quota
or connectivity errors fail admission closed (503). PostgreSQL wake-up latency
can exceed our short pool/telemetry budgets, producing temporary 503 responses
or reported telemetry write failures. Telemetry remains best effort.

Free hosting is for an intermittent demo: Render sleeps after 15 idle minutes;
Neon compute sleeps after five, while stored data survives. Quotas, cold starts,
and Upstash inactivity policies can interrupt access. The current Upstash pricing
page advertises 500K commands/month; Lua's internal commands count too. Check
actual account allowances and retain independent database exports. No hosted
backup, restore, or rollback drill has been completed.

The web service's HTML is publicly reachable, but paid API access is key-gated.
Render's network-private service type is different: it has no public browser URL.
Add an outer identity/IP-access gate if the entire UI must be private. No keys
should appear in URLs, logs, CI artifacts, or screenshots.

For future releases, verify CI results, startup migrations, `/readyz`, UI/API
routing, authentication/revocation, security headers, and log redaction. Hosted
admission saturation and dependency-outage drills remain pending; use **fake
providers** for those checks. Check provider-account budget controls and the
kill switch before paid use. Additional live-provider calls require explicit
owner authorization; none are required by CI.

### Backup and rollback procedure

1. Before a release, identify the last known-good build and configuration, keep
   auto-deploys off, and confirm the database's actual backup/PITR availability,
   retention, and recovery objectives. A named Docker volume is not a backup.
2. Create a logical export or confirmed recovery point. Restore into a **new,
   isolated database**, validate migrations, row totals, analytics, and managed
   key/revocation state, then rehearse switching a nonproduction service to it.
   Never test a destructive restore against the active database.
3. For rollback, disable paid access and independently suspend/restrict provider
   credentials first. Roll back the application to the retained Render build;
   do not rebuild an old commit and assume identical artifacts. Database changes
   are not undone by application rollback. Keep migrations backward compatible;
   restore/switch databases only after checking data loss and schema compatibility.
4. Recheck the kill switch and secrets after rollback: Render can restore
   service-level environment variables, while environment-group values and disks
   have different rollback behavior. A database restore can resurrect revoked
   keys; reconcile/revoke or rotate them before re-enabling paid access.
5. Validate readiness, authenticated analytics, revocation, and fake-provider
   requests, then enable paid calls only by owner decision. Retain the original
   database until the restored instance is verified. Do not flush shared Redis;
   verify admission state and cache isolation after recovery.

These are procedures to verify, not completed hosted backup/rollback drills.
Platform references: [Render web services](https://render.com/docs/web-services),
[Blueprint configuration](https://render.com/docs/blueprint-spec),
[health checks](https://render.com/docs/health-checks),
[private services](https://render.com/docs/private-services),
[free hosting limits](https://render.com/docs/free),
[Neon TLS](https://neon.com/docs/connect/connect-securely),
[Neon pooling](https://neon.com/docs/connect/connection-pooling),
[Upstash compatibility](https://upstash.com/docs/redis/overall/compatibility),
[Upstash eviction](https://upstash.com/docs/redis/features/eviction), and
[rollbacks](https://render.com/docs/rollbacks).

## Author

Mohib Zaidi
