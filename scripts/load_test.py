"""Small local load check with fake providers and real Redis/PostgreSQL.

Uses a random gateway key. Removes only its own telemetry rows and Redis keys.
No provider SDK or credential is used.
"""

import asyncio
import hashlib
import json
import math
import os
import time
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import psycopg
import redis
import redis.asyncio as async_redis
from psycopg.rows import dict_row

from gateway.app import create_app
from gateway.config import ModelAlias
from gateway.pricing import PriceBook, TokenPrice
from gateway.providers.base import Generation, ProviderError, Usage
from gateway.redis_store import RedisPolicy, RedisStore, make_cache_key
from gateway.providers.base import GenerationOptions, Message
from gateway.routing import RetryPolicy
from gateway.telemetry import PostgresTelemetryRepository


DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL", "postgresql://gateway:gateway-dev-password@127.0.0.1:5433/gateway"
)
REDIS_URL = os.getenv("TEST_REDIS_URL", "redis://127.0.0.1:6379/0")


class FakePrimary:
    name = "openai"

    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, messages, *, model, options):
        self.calls += 1
        await asyncio.sleep(0.01)
        if any(message.content.startswith("recover-") for message in messages):
            raise ProviderError("server_error")
        return Generation("fixture answer", model, "stop", Usage(12, 6))


class FakeSecondary:
    name = "anthropic"

    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, messages, *, model, options):
        self.calls += 1
        await asyncio.sleep(0.01)
        return Generation("fixture fallback answer", model, "stop", Usage(12, 6))


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(fraction * len(ordered)) - 1)], 3)


async def run() -> None:
    run_id = uuid4().hex
    key = "load-fixture-" + run_id
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    primary, secondary = FakePrimary(), FakeSecondary()
    store = RedisStore(
        async_redis.from_url(REDIS_URL, decode_responses=True),
        RedisPolicy(requests_per_window=1000, window_seconds=60, cache_ttl_seconds=300),
    )
    app = create_app(
        gateway_api_key=key,
        providers={"openai": primary, "anthropic": secondary},
        model_aliases={"load-test": ModelAlias("openai", {
            "openai": "load-openai", "anthropic": "load-anthropic"
        })},
        retry_policy=RetryPolicy(max_retries=0),
        redis_store=store,
        price_book=PriceBook("load-fixture-v1", {
            ("openai", "load-openai"): TokenPrice(Decimal("0.50"), Decimal("1.50")),
            ("anthropic", "load-anthropic"): TokenPrice(Decimal("1.00"), Decimal("3.00")),
        }),
        telemetry_repository=PostgresTelemetryRepository(DATABASE_URL),
    )
    headers = {"Authorization": "Bearer " + key}
    warm_bodies = [
        {"model": "load-test", "messages": [{"role": "user", "content": f"cache-{run_id}-{index}"}],
         "temperature": 0, "cache": True}
        for index in range(10)
    ]
    normal_bodies = [
        {"model": "load-test", "messages": [{"role": "user", "content": f"normal-{run_id}-{index}"}]}
        for index in range(16)
    ]
    recovery_bodies = [
        {"model": "load-test", "messages": [{"role": "user", "content": f"recover-{run_id}-{index}"}]}
        for index in range(4)
    ]
    results: list[tuple[float, int, dict]] = []
    request_ids: list[UUID] = []
    cache_keys = [
        make_cache_key(
            key_hash=key_hash, provider="openai", model="load-openai",
            messages=[Message("user", body["messages"][0]["content"])],
            options=GenerationOptions(temperature=0),
        )
        for body in warm_bodies
    ]

    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://local-test", timeout=10
            ) as client:
                semaphore = asyncio.Semaphore(5)

                async def send(body: dict) -> None:
                    async with semaphore:
                        started = time.perf_counter()
                        response = await client.post("/v1/chat", json=body, headers=headers)
                        elapsed_ms = (time.perf_counter() - started) * 1000
                        request_ids.append(UUID(response.headers["X-Request-ID"]))
                        results.append((elapsed_ms, response.status_code, response.json()))

                started = time.perf_counter()
                for body in warm_bodies:
                    await send(body)
                await asyncio.gather(*(
                    send(body) for body in (warm_bodies + normal_bodies + recovery_bodies)
                ))
                total_seconds = time.perf_counter() - started
                summary = await client.get("/analytics/summary?window=1h", headers=headers)
                assert summary.status_code == 200

        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
            rows = connection.execute(
                "SELECT request_id, input_tokens, output_tokens, cache_hit, estimated_cost_usd, "
                "final_provider FROM request_telemetry WHERE request_id = ANY(%s)",
                (request_ids,),
            ).fetchall()
        count = len(results)
        cache_hits = sum(bool(body.get("cache_hit")) for _, _, body in results)
        errors = sum(status >= 400 for _, status, _ in results)
        recovered = sum(
            status == 200 and body.get("final_provider") == "anthropic"
            for _, status, body in results
        )
        latencies = [latency for latency, _, _ in results]
        cost = sum((row["estimated_cost_usd"] or Decimal("0")) for row in rows)
        assert count == len(rows) == 40
        assert cache_hits == 10 and errors == 0 and recovered == 4
        assert (primary.calls, secondary.calls) == (30, 4)
        assert sum(row["input_tokens"] or 0 for row in rows) == 360
        assert sum(row["output_tokens"] or 0 for row in rows) == 180
        assert cost == Decimal("0.000510000000")
        assert summary.json()["request_count"] == 40
        print(json.dumps({
            "workload": "10 cache warm-ups, 10 cache repeats, 16 unique requests, 4 transient failures with fallback",
            "requests": count,
            "peak_concurrency": 5,
            "elapsed_seconds": round(total_seconds, 3),
            "throughput_requests_per_second": round(count / total_seconds, 2),
            "response_latency_p50_ms": percentile(latencies, 0.5),
            "response_latency_p95_ms": percentile(latencies, 0.95),
            "error_rate": errors / count,
            "cache_hit_rate": cache_hits / count,
            "fallback_recovery": f"{recovered}/4",
            "provider_calls": {"fake_openai": primary.calls, "fake_anthropic": secondary.calls},
            "postgres_rows_verified": len(rows),
            "input_tokens": 360,
            "output_tokens": 180,
            "estimated_fixture_cost_usd": format(cost, "f"),
        }, indent=2))
    finally:
        if request_ids:
            with psycopg.connect(DATABASE_URL) as connection:
                connection.execute(
                    "DELETE FROM request_telemetry WHERE request_id = ANY(%s)",
                    (request_ids,),
                )
        await store.aclose()
        with redis.Redis.from_url(REDIS_URL) as client:
            client.delete("gateway:rate:v1:" + key_hash, *cache_keys)


if __name__ == "__main__":
    asyncio.run(run())
