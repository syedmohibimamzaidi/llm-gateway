# LLM Gateway — local MVP

This service implements the gateway, Redis cache and rate limit, PostgreSQL telemetry, React analytics dashboard, and gateway key management from `LLM_Gateway_MVP.txt`. The Compose stack is for local use; no deployment is included.

## Quick start with the existing local containers

The current `gateway-redis` and `gateway-postgres` containers use host ports 6379 and 5433. Leave them and their volumes in place. If they are stopped, start them with `docker start gateway-redis gateway-postgres`. From the repository root, run:

```sh
docker exec gateway-postgres pg_isready -U gateway -d gateway
docker exec -i gateway-postgres psql -v ON_ERROR_STOP=1 -U gateway -d gateway < migrations/001_request_telemetry.sql
docker exec -i gateway-postgres psql -v ON_ERROR_STOP=1 -U gateway -d gateway < migrations/002_gateway_keys.sql
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/uvicorn gateway.main:app --host 127.0.0.1 --port 8000
```

Keep the API terminal open. In a second terminal, from the repository root:

```sh
cd dashboard
npm ci
npm run dev
```

Open `http://127.0.0.1:5173` and enter the `GATEWAY_API_KEY` value from your existing `.env`. `gateway.main` loads `.env` before creating the API, so its values override stale exports in the launching shell. The dashboard uses the Vite proxy to reach the API at port 8000. These commands do not call a provider by themselves. If the API or dashboard is already running on these ports, restart an older `gateway.app:app` process to pick up this entrypoint and current configuration.

## Isolated Docker Compose startup

For a new checkout, copy `.env.example` to `.env` and fill in a gateway key and the model alias configuration. Keep an existing `.env` unchanged. Then run from the repository root:

```sh
docker compose -f compose.yaml up --build -d
docker compose -f compose.yaml ps
```

Open `http://127.0.0.1:5174`. The Compose API is at port 8001, PostgreSQL at 5434, and Redis at 6380. This stack uses its own PostgreSQL volume and applies both SQL migrations in filename order before starting the API, so it does not replace or change the existing containers on ports 5433 and 6379. It reads `.env` for the gateway and provider configuration while overriding the database, Redis, and price-file paths inside the containers. The image includes `prices.live.json`, and Compose selects that file so the documented live model IDs have price estimates. Stop only this stack with `docker compose -f compose.yaml stop`; its containers and database volume remain available for the next `up`.

```mermaid
flowchart LR
    Browser[React dashboard] -->|Bearer gateway key| API[FastAPI analytics]
    Client[Chat client] -->|POST /v1/chat| Gateway[FastAPI auth and rate limit]
    Gateway --> Redis[(Redis rate counters and opt-in cache)]
    Gateway --> Router[Retry and fallback router]
    Router --> OpenAI[OpenAI adapter]
    Router --> Anthropic[Anthropic adapter]
    Gateway --> Telemetry[(PostgreSQL request telemetry)]
    API --> Telemetry
```

## Run locally

Python 3.11 or newer is required. These commands use Python 3.12, which was used for the test run:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
```

Set the variables shown in `.env.example` with the credentials and model IDs for your environment. `GATEWAY_API_KEY` authenticates callers to this service. `OPENAI_API_KEY` and `ANTHROPIC_API_KEY` authenticate the service to their respective providers. Start with `gateway.main:app`: it loads `.env` before app creation and gives file values precedence over exported shell variables. Set `GATEWAY_ENV_FILE` to use a different file. In Compose, `.env` is excluded from the image and Compose injects its values into the container environment. The legacy `gateway.app:app --env-file .env` command may keep stale exported values because Uvicorn does not override them.

Run Redis and set `REDIS_URL` to its connection URL (default `redis://localhost:6379/0`). Every authenticated chat request needs Redis for rate limiting. `GATEWAY_RATE_LIMIT` defaults to 60 requests per key in a `GATEWAY_RATE_WINDOW_SECONDS` window of 60 seconds. This is a fixed window starting with the first request. Cache entries expire after `GATEWAY_CACHE_TTL_SECONDS`, which defaults to 60 seconds.

For local PostgreSQL, leave the existing Redis container on port 6379 and use host port 5433 for PostgreSQL:

```sh
docker run -d --name gateway-postgres \
  -e POSTGRES_USER=gateway -e POSTGRES_PASSWORD=gateway-dev-password \
  -e POSTGRES_DB=gateway -p 127.0.0.1:5433:5432 \
  -v gateway_pgdata:/var/lib/postgresql/data postgres:16
docker exec gateway-postgres pg_isready -U gateway -d gateway
```

If the `gateway-postgres` container already exists but is stopped, use `docker start gateway-postgres` instead of `docker run`. Keep the existing `gateway-redis` container running.

Once `pg_isready` reports that PostgreSQL accepts connections, apply both migrations in order:

```sh
docker exec -i gateway-postgres psql -v ON_ERROR_STOP=1 -U gateway -d gateway < migrations/001_request_telemetry.sql
docker exec -i gateway-postgres psql -v ON_ERROR_STOP=1 -U gateway -d gateway < migrations/002_gateway_keys.sql
```

Set `DATABASE_URL=postgresql://gateway:gateway-dev-password@localhost:5433/gateway` before starting the app. Both SQL migrations are transactional and can be run again safely. Check the saved telemetry rows with:

```sh
docker exec -it gateway-postgres psql -U gateway -d gateway \
  -c 'SELECT request_id, model_alias, final_provider, input_tokens, output_tokens, estimated_cost_usd, outcome, cache_hit FROM request_telemetry ORDER BY created_at DESC LIMIT 10;'
```

For local dashboard testing, `prices.json` and `prices.example.json` contain **test pricing only**: numeric USD-per-million-token fixture rates and version `local-test-pricing-v1`. These are not provider prices or billing rates. `prices.live.json` contains standard text-token rates verified on 2026-10-03 from the [OpenAI GPT-4.1 mini](https://developers.openai.com/api/docs/models/gpt-4.1-mini), [OpenAI GPT-4.1](https://developers.openai.com/api/docs/models/gpt-4.1), [Claude Haiku 4.5](https://platform.claude.com/docs/en/models/haiku-4-5/overview), and [Claude Sonnet 4.6](https://platform.claude.com/docs/en/models/sonnet-4-6/overview) model pages. Both the existing local `.env` and the isolated Compose stack select this live price file. If your `GATEWAY_MODEL_ALIASES` uses different actual model IDs, update the selected price file to match; otherwise requests still work but their estimated cost is null. Prices are estimates and exclude provider-side prompt-cache discounts and taxes. Recheck official rates before relying on them for billing. Each input and output rate must be a nonnegative number or numeric string. An invalid configured price file prevents API startup. With no price file configured, requests work but `estimated_cost_usd` is null and `price_version` is `unconfigured`.

`GATEWAY_MODEL_ALIASES` is a JSON object. Each alias has a `default_provider` and a `models` map containing the provider-specific model IDs allowed for that alias. Both `fast` and `smart` must be configured. The provider override works only when the selected alias includes a model for that provider. Provider keys can be omitted if that provider is not used. A fallback requires both a model mapping and a credential for the second provider.

For live local use, `.env.example` and the existing `.env` map `fast` to pinned `gpt-4.1-mini-2025-04-14` and `claude-haiku-4-5-20251001`, and `smart` to pinned `gpt-4.1-2025-04-14` and `claude-sonnet-4-6`. To enable both adapters, fill in only `OPENAI_API_KEY` and `ANTHROPIC_API_KEY` with active API credentials in `.env`; keep `GATEWAY_API_KEY` as the existing caller credential. The existing `REDIS_URL`, `DATABASE_URL`, model aliases, and `GATEWAY_PRICE_CONFIG_PATH=prices.live.json` are configured for the local containers. Restart the API after changing `.env`; `gateway.main:app` loads it at process startup. A provider may still require account credit, model access, or a higher usage tier. The gateway intentionally returns a safe provider error without exposing the upstream error body or credentials.

## Gateway key management

The existing `GATEWAY_API_KEY` in `.env` remains the bootstrap management key and continues to work for chat and its own analytics. Only this configured key may create, list, or revoke managed keys. Managed keys can call `/v1/chat` and view only their own analytics; their calls to `/api-keys` return HTTP 403. Invalid or revoked keys return HTTP 401. PostgreSQL is required for managed keys and key management: when it is unavailable, managed-key authentication and management return HTTP 503, while the configured key still authenticates. Apply `002_gateway_keys.sql` before using these endpoints.

With `GATEWAY_API_KEY` available in your shell, create, list, or revoke a key using the API port for your setup (8000 for the existing local API, 8001 for Compose):

```sh
curl -X POST http://127.0.0.1:8000/api-keys \
  -H "Authorization: Bearer $GATEWAY_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"name":"local-client"}'
curl http://127.0.0.1:8000/api-keys \
  -H "Authorization: Bearer $GATEWAY_API_KEY"
curl -X DELETE http://127.0.0.1:8000/api-keys/KEY_ID_FROM_LIST \
  -H "Authorization: Bearer $GATEWAY_API_KEY"
```

The `POST` response contains a random `gw_...` bearer key **once**; save it securely at creation. `GET` and `DELETE` return only the key's UUID, name, creation time, and revocation time. Repeating `DELETE` for the same UUID returns its original revocation time; an unknown UUID returns 404. The `gateway_keys` table holds only a SHA-256 hash of each managed key, its UUID, name, and timestamps. Neither analytics responses nor application log messages contain raw keys or key hashes. Each authenticated key has its own Redis rate-limit counter, cache namespace, and telemetry scope. A revoked key loses access on its next authentication check; existing telemetry remains in PostgreSQL for audit and is no longer accessible with that revoked key. The configured management key is controlled only by `.env`, so rotating it is a configuration and restart operation.

`GATEWAY_MAX_RETRIES` defaults to 2 and accepts 0–5 additional attempts per provider. `GATEWAY_RETRY_BASE_DELAY_SECONDS` defaults to 0.1 and accepts 0–5 seconds. Retry delays double after each failed attempt. Each provider HTTP client uses a 30-second inactivity timeout. `GATEWAY_REQUEST_DEADLINE_SECONDS` defaults to 45 and accepts a value greater than 0 and at most 60 seconds. This wall-clock deadline covers all provider calls and retry waits for one chat request.

Start the service with `.venv/bin/uvicorn gateway.main:app --host 127.0.0.1 --port 8000`. Example request:

```sh
curl -X POST http://127.0.0.1:8000/v1/chat \
  -H "Authorization: Bearer $GATEWAY_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"fast","messages":[{"role":"user","content":"Say hello"}],"max_tokens":128,"temperature":0,"cache":true}'
```

The same request can select Anthropic by adding `"provider":"anthropic"` when `fast` has an Anthropic model mapping. The request accepts `model` (alias), `messages`, optional `max_tokens` (default 1024), optional `temperature` (0–1), optional `provider` override, and optional `cache` (default false). Messages have `system`, `user`, or `assistant` roles; system messages must come first. The response contains a request ID, selected provider, actual model, text, normalized finish reason, input/output token usage, `initial_provider`, `final_provider`, `retry_count`, `failure_reason`, an `attempts` list, and `cache_hit`.

Rate limiting runs before cache lookup, so cache hits count toward the key's limit. The next request after the configured threshold returns HTTP 429. Caching is used only when `cache` is true, `temperature` is explicitly zero, the final message is from the user, and the provider succeeds without retry or fallback. Other requests run normally without caching. Cache keys include the caller key hash, selected provider, configured provider model ID, messages, and generation parameters; the key itself is hashed so prompts are not visible in Redis key names. A cache hit returns a new request ID with `cache_hit: true` and an empty `attempts` list, without calling a provider. An eligible success stores its response text and metadata in Redis for the configured TTL; ordinary requests do not store prompt or response bodies.

Only provider timeouts, HTTP 429, and HTTP 5xx are retried. Without an explicit provider override, a configured second provider gets its own bounded retry sequence after the first provider exhausts its retries. Setting `provider` pins the request to that provider: eligible failures can still retry there, but cannot call the other provider. Other errors stop immediately. `retry_count` counts repeated calls to a provider that actually start; the first fallback call is a new attempt, so it does not add to that count. On a recovered response, `failure_reason` is the last transient reason; on a final failure, it is the terminal reason. Each `attempts` entry records the provider, model, and a safe reason code or `null` for success. Failed requests return the same route metadata in the HTTP 502 `detail` object. If the deadline expires, the in-flight provider call is cancelled and the service returns HTTP 504 with `failure_reason: "deadline_exceeded"`; no further attempt starts.

Each `POST /v1/chat` authenticates before body validation and gets an `X-Request-ID` response header. A valid key on a malformed request is therefore retained in its telemetry row, while a missing or invalid key returns 401 even with a malformed body. PostgreSQL rows include the authenticated key identifier (SHA-256), model alias when validation succeeds, initial and final provider, actual model, reported token counts, latency, status, outcome, retry count, failure reason, cache hit, attempt metadata, price version, and estimated USD cost. The table has a timestamp; it contains no raw messages, response text, API keys, or authorization headers. The price estimate uses the final provider's reported input and output tokens and the configured rates. It may differ from provider billing, especially if a failed attempt consumed unreported tokens. On a cache hit, the response repeats the cached usage for compatibility, while the new telemetry row records zero provider tokens and zero incremental cost. On failures with no reported usage, token counts and cost are null.

Every chat response includes `X-Telemetry-Status: stored`, `failed`, or `disabled`. `disabled` means `DATABASE_URL` is unset. If PostgreSQL is temporarily unavailable or a write exceeds 750 ms, the gateway logs a warning with the request ID and exception type, sets `failed`, and still returns the chat result. This is best-effort telemetry: there is no durable queue or replay, so a failed write can permanently lose that row and make dashboard totals incomplete. An unexpected server exception may have no response header; check server logs in that case. Apply both migrations before enabling PostgreSQL telemetry.

## Analytics dashboard

The authenticated `GET /analytics/summary`, `GET /analytics/providers`, and `GET /requests` endpoints read PostgreSQL telemetry for the authenticated gateway key only. Each accepts `window=1h|24h|7d|30d` (default `24h`). `/requests` also accepts `limit` (1–100, default 25) and `offset` (default 0). Summary and provider results include request, success, error and cache-hit counts and rates; input/output token totals; estimated USD cost; unpriced successful request count; and mean/p95 latency. The provider breakdown groups by final provider, with `unrouted` for requests that never selected one. Rates use all authenticated requests in the selected window as the denominator. Cache hits contribute zero new tokens and cost. History contains selected request metadata only. None of these responses includes gateway key hashes, API keys, raw messages, or response text. Analytics reads return HTTP 503 when PostgreSQL is not configured or available.

Start the API with `GATEWAY_API_KEY` and `DATABASE_URL` set, along with the existing Redis and optional provider configuration. In a second terminal, use Node.js 22.12 or newer:

```sh
cd dashboard
npm ci
npm run dev
```

Open `http://127.0.0.1:5173` and enter the configured `GATEWAY_API_KEY` or an active managed gateway key. You can paste the configured value alone or its complete `GATEWAY_API_KEY=...` line from `.env`; surrounding quotes are removed. Do not use `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`. The dashboard checks authentication before loading the three views. It keeps the key in page memory and sends it in the `Authorization` header; it does not place the key in a URL or browser storage. If an older dashboard tab holds an incorrect key, click **Disconnect** and reconnect. The Vite development server proxies `/analytics` and `/requests` to the API at `127.0.0.1:8000`, so both servers must be running. `npm test` checks key-input handling and `npm run build` checks the frontend production build. If no requests were made with the authenticated key, the dashboard shows an empty window.

`/health` is available without authentication. An invalid gateway key returns 401; invalid input or alias returns 422; missing provider configuration returns 503. Provider failures return 502, including provider credential failures, and the overall deadline returns 504. If Redis is unavailable when the rate-limit check runs, the service returns HTTP 503 before cache lookup or any provider call. This protects the provider budget. If a cache read or write fails after a successful rate-limit check, the request proceeds without caching. Redis connects lazily, so `/health` can still respond while Redis is unavailable.

Run `.venv/bin/python -m pytest -q` for the dependency-free checks; the live-service tests skip unless `TEST_DATABASE_URL` and `TEST_REDIS_URL` are set. To run the full suite against the existing local containers with fake providers:

```sh
TEST_DATABASE_URL=postgresql://gateway:gateway-dev-password@127.0.0.1:5433/gateway \
TEST_REDIS_URL=redis://127.0.0.1:6379/0 \
  .venv/bin/python -m pytest -q
cd dashboard
npm test
npm run build
```


The fake-provider load workload and final verification results are added with the verification tooling.
