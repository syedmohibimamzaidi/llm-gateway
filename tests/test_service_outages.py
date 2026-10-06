"""Exercise the real Redis/PostgreSQL clients against unavailable local endpoints."""

from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.config import ModelAlias
from gateway.pricing import PriceBook
from gateway.providers.base import Generation, Usage
from gateway.telemetry import PostgresTelemetryRepository


HEADERS = {"Authorization": "Bearer outage-fixture-key"}
BODY = {"model": "fast", "messages": [{"role": "user", "content": "fixture"}]}


class FakeProvider:
    name = "openai"

    def __init__(self):
        self.calls = 0

    async def generate(self, messages, *, model, options):
        self.calls += 1
        return Generation("fixture response", model, "stop", Usage(2, 1))


class AllowingStore:
    async def allow(self, key_hash):
        return True

    async def get_cached(self, cache_key):
        return None

    async def put_cached(self, cache_key, response_json):
        pass


class NoopTelemetry:
    async def record(self, event):
        pass


def make_app(provider, store, repository):
    return create_app(
        gateway_api_key="outage-fixture-key",
        providers={"openai": provider},
        model_aliases={"fast": ModelAlias("openai", {"openai": "fixture-model"})},
        redis_store=store,
        price_book=PriceBook("outage-test-pricing", {}),
        telemetry_repository=repository,
    )


def test_redis_connection_failure_blocks_request_before_provider(monkeypatch) -> None:
    provider = FakeProvider()
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    with TestClient(make_app(provider, None, NoopTelemetry())) as client:
        response = client.post("/v1/chat", json={**BODY, "cache": True, "temperature": 0},
                               headers=HEADERS)
    assert response.status_code == 503
    assert response.json()["detail"] == "Rate limiter unavailable"
    assert response.headers["X-Telemetry-Status"] == "stored"
    assert provider.calls == 0


def test_postgres_connection_failure_reports_telemetry_and_analytics(caplog) -> None:
    provider = FakeProvider()
    repository = PostgresTelemetryRepository(
        "postgresql://fixture:fixture@127.0.0.1:1/fixture"
    )
    with TestClient(make_app(provider, AllowingStore(), repository)) as client:
        chat = client.post("/v1/chat", json=BODY, headers=HEADERS)
        analytics = client.get("/analytics/summary", headers=HEADERS)
        managed = client.post("/v1/chat", json=BODY, headers={
            "Authorization": "Bearer gw_outage_fixture"
        })
        invalid = client.post("/v1/chat", json=BODY, headers={
            "Authorization": "Bearer invalid-fixture"
        })
    assert chat.status_code == 200
    assert managed.status_code == 503
    assert invalid.status_code == 401
    for response in (chat, managed, invalid):
        assert response.headers["X-Telemetry-Status"] == "failed"
        assert response.headers["X-Request-ID"] in caplog.text
    assert analytics.status_code == 503
    assert analytics.json()["detail"] == "Analytics unavailable"
    assert provider.calls == 1
    assert "outage-fixture-key" not in caplog.text
