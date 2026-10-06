# Local final verification — 2026-10-05

All automated checks and the local HTTP checks passed. Every provider response in this run was fake. No paid calls, push, or deployment occurred.

## Checks and evidence

| Check | Result |
| --- | --- |
| Missing, incorrect and revoked keys | 401, including malformed chat bodies; rejected requests made no provider call. Managed keys cannot perform management operations. |
| Malformed requests and provider authentication/validation errors | Malformed input returns 422 with a valid key. Nontransient provider errors receive no retry or fallback. Authentication precedes chat body validation. |
| Per-key analytics, cache and rate limit | Real Redis/PostgreSQL tests passed. Identical requests under different keys each made their own provider call. Cached requests counted toward the limit; the fourth managed-key request returned 429 while the other key continued. Analytics reported separate totals of 4/2 and 3/1 requests/cache hits. |
| Cache expiry | A real one-second Redis TTL expired; the next eligible request called the fake provider again. |
| Retries, fallback and strict overrides | HTTP checks verified recovery after a rate limit, fallback after exhausted server-error retries, both providers failing, and an explicit override staying on its selected provider. Attempt paths and retry counts matched expectations. |
| Deadlines | Existing tests verify cancellation during backoff and a hanging fallback, preventing further attempts. A temporary 50 ms routing deadline returned HTTP 504 in 55.137 ms in the Compose HTTP check. Normal configuration was preserved. |
| Redis outage | Stopped only the isolated Redis container. Chat returned 503 before any provider call, and PostgreSQL stored the failed request telemetry. Unit tests also cover cache-only read/write failures proceeding without caching after a successful rate-limit check. |
| PostgreSQL outage | Stopped only the isolated PostgreSQL container. Configured-key chat returned 200 with `X-Telemetry-Status: failed`; analytics, management and managed-key lookup returned 503. An incorrect nonmanaged key still returned 401. |
| Telemetry reporting and recovery | All 3 failed writes emitted warnings with their request IDs. After restart, all 16 persisted fixture rows remained and none of the 3 failed-write rows appeared. Analytics recovered. Logs contained no gateway/provider secrets, their hashes, or raw fixture messages. |
| Fresh Compose startup | Project `gateway-finalcheck-20261005` created a new PostgreSQL volume. Both migrations completed before API startup. Normal API health, dashboard HTML, all authenticated analytics endpoints and their Vite proxies passed; missing/incorrect keys returned 401. |
| Migrations and pricing | Replaying migrations preserved all fixture rows and both migration records. The API image contained valid numeric pricing for every configured alias/provider model; `.env` was absent from the image. |
| Dashboard analytics | Direct API and Vite proxy agreed on summary, provider breakdown and request history. Four successful smoke requests produced 1 cache hit, 21 input tokens, 9 output tokens and $0.000037200000 estimated fixture cost at the configured rates. No raw messages, secrets or key hashes appeared in analytics responses. |

## Automated results

- Python: **69 passed**, with real Redis on port 6379 and PostgreSQL on port 5433; no service tests skipped.
- Dashboard: **3 passed**; `npm run build` succeeded.
- A nonblocking Starlette TestClient deprecation warning remains.

Regression additions are in `tests/test_service_outages.py`, `tests/test_api_keys_services.py` and `tests/test_api_keys.py`. They exercise actual unavailable Redis/PostgreSQL clients, real per-key isolation/expiry and revoked/incorrect-key rejection before body validation. The two initially failing real-service tests were caused by sandbox-denied localhost connections. Both passed when rerun with localhost access; no application fix was required for those failures.

To reproduce the automated checks:

```sh
TEST_DATABASE_URL=postgresql://gateway:gateway-dev-password@127.0.0.1:5433/gateway \
TEST_REDIS_URL=redis://127.0.0.1:6379/0 \
  .venv/bin/python -m pytest -q

cd dashboard
npm test
npm run build
```

## Fake-provider load check

The existing load script ran against the isolated Compose services on ports 5434/6380. Its workload was 10 cache warm-ups, 10 immediate repeats, 16 unique requests and 4 transient failures recovered by fallback, with peak concurrency 5.

| Measurement | Result |
| --- | --- |
| Requests / verified database rows | 40 / 40 |
| Elapsed time / throughput | 0.273 s / 146.61 requests/s |
| Response p50 / p95 | 14.622 ms / 23.855 ms |
| HTTP error rate | 0% |
| Cache-hit rate | 25% |
| Fallback recovery | 4/4 |
| Fake OpenAI / Anthropic calls | 30 / 4 |
| Input / output tokens | 360 / 180 |
| Estimated fixture cost | $0.000510000000 |

These are local fixture measurements, not live-provider latency or billed costs. The load script removed its own rows and Redis keys.

## Preservation and limits

The original PostgreSQL/Redis containers were never stopped. Integration tests removed their own randomized keys, rows and cache entries. The new Compose project's containers, network and database volume were removed after verification. `.env` matched its pre-check checksum, and normal provider configuration was preserved; the fake-provider override existed only in a temporary local harness.

Telemetry remains best effort: a write failure is reported by `X-Telemetry-Status: failed` and a warning, but there is no durable queue or replay. `/health` is a liveness check and remains 200 during service outages. The default 45-second deadline bounds provider attempts and backoff; authentication, Redis and telemetry have separate time bounds.

Interactive browser rendering, clicking through time ranges/pagination, and browser failure-state presentation were not exercised. Dashboard build, authentication helpers, HTTP serving, proxy authentication and underlying analytics results were verified. Live-provider credentials/availability and deployment were not tested in this run. Git staging/history could not be reviewed because this directory has no Git repository.
