"""Real Redis/PostgreSQL check; set TEST_REDIS_URL and TEST_DATABASE_URL."""

import hashlib
import os
from decimal import Decimal
from uuid import UUID, uuid4

import psycopg
import pytest
import redis
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.config import ModelAlias
from gateway.pricing import PriceBook, TokenPrice
from gateway.providers.base import Generation, GenerationOptions, Message, Usage
from gateway.redis_store import make_cache_key
from gateway.routing import RetryPolicy
from gateway.telemetry import PostgresTelemetryRepository


DATABASE_URL = os.getenv("TEST_DATABASE_URL")
REDIS_URL = os.getenv("TEST_REDIS_URL")


class FakeProvider:
    name = "openai"

    def __init__(self):
        self.calls = 0

    async def generate(self, messages, *, model, options):
        self.calls += 1
        return Generation("fixture response", model, "stop", Usage(12, 6))


@pytest.mark.skipif(
    not DATABASE_URL or not REDIS_URL,
    reason="Set TEST_DATABASE_URL and TEST_REDIS_URL for the live services check",
)
def test_real_redis_cache_rate_limit_and_postgres_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    key = "services-fixture-" + uuid4().hex
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    prompt = "services-prompt-" + uuid4().hex
    provider = FakeProvider()
    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    monkeypatch.setenv("GATEWAY_RATE_LIMIT", "3")
    monkeypatch.setenv("GATEWAY_RATE_WINDOW_SECONDS", "60")
    monkeypatch.setenv("GATEWAY_CACHE_TTL_SECONDS", "60")
    model = "fixture-openai-model"
    app = create_app(
        gateway_api_key=key,
        providers={"openai": provider},
        model_aliases={"fast": ModelAlias("openai", {"openai": model})},
        retry_policy=RetryPolicy(max_retries=0),
        price_book=PriceBook("day7-fixture", {
            ("openai", model): TokenPrice(Decimal("0.50"), Decimal("1.50"))
        }),
        telemetry_repository=PostgresTelemetryRepository(DATABASE_URL),
    )
    first = {"model": "fast", "messages": [{"role": "user", "content": prompt}],
             "temperature": 0, "cache": True}
    different = {**first, "messages": [{"role": "user", "content": prompt + "-different"}]}
    headers = {"Authorization": "Bearer " + key}
    responses = []
    ids = []
    options = GenerationOptions(temperature=0)
    cache_keys = [
        make_cache_key(
            key_hash=key_hash, provider="openai", model=model,
            messages=[Message("user", content)], options=options,
        )
        for content in (prompt, prompt + "-different")
    ]
    try:
        with TestClient(app) as client:
            responses = [client.post("/v1/chat", json=body, headers=headers)
                         for body in (first, first, different, first)]
        ids = [UUID(response.headers["X-Request-ID"]) for response in responses]
        assert [response.status_code for response in responses] == [200, 200, 200, 429]
        assert [response.json()["cache_hit"] for response in responses[:3]] == [False, True, False]
        assert provider.calls == 2
        with psycopg.connect(DATABASE_URL) as connection:
            rows = connection.execute(
                "SELECT request_id, input_tokens, output_tokens, cache_hit, "
                "estimated_cost_usd, outcome FROM request_telemetry "
                "WHERE request_id = ANY(%s)", (ids,),
            ).fetchall()
        assert len(rows) == 4
        by_id = {row[0]: row for row in rows}
        assert [(by_id[request_id][1], by_id[request_id][2], by_id[request_id][3])
                for request_id in ids] == [
                    (12, 6, False), (0, 0, True), (12, 6, False), (None, None, False)
                ]
        assert [by_id[request_id][5] for request_id in ids] == [
            "success", "cache_hit", "success", "rate_limited"
        ]
        assert sum((by_id[request_id][4] or Decimal("0")) for request_id in ids) == Decimal("0.000030000000")
    finally:
        if ids:
            with psycopg.connect(DATABASE_URL) as connection:
                connection.execute("DELETE FROM request_telemetry WHERE request_id = ANY(%s)", (ids,))
        with redis.Redis.from_url(REDIS_URL) as client:
            client.delete("gateway:rate:v1:" + key_hash, *cache_keys)
