import hashlib
import json
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from gateway.app import create_app
from gateway.config import ModelAlias
from gateway.pricing import PriceBook, TokenPrice
from gateway.providers.base import Generation, GenerationOptions, Message, ProviderError, Usage
from gateway.routing import Attempt, RetryPolicy
from gateway.telemetry import INSERT_COLUMNS, INSERT_SQL, PostgresTelemetryRepository, RequestTelemetry


KEY = "test-gateway-secret"
HEADERS = {"Authorization": f"Bearer {KEY}"}
BODY = {"model": "fast", "messages": [{"role": "user", "content": "sensitive prompt"}]}
ALIASES = {"fast": ModelAlias("openai", {"openai": "fixture-model"})}
PRICES = PriceBook(
    version="fixture-v1",
    models={("openai", "fixture-model"): TokenPrice(Decimal("0.20"), Decimal("0.80"))},
)


class Provider:
    name = "openai"

    def __init__(self) -> None:
        self.calls = 0
        self.fail = False

    async def generate(
        self, messages: list[Message], *, model: str, options: GenerationOptions
    ) -> Generation:
        self.calls += 1
        if self.fail:
            raise ProviderError("server_error")
        return Generation("answer body", model, "stop", Usage(100, 50))


class Store:
    def __init__(self, limit: int = 60) -> None:
        self.limit = limit
        self.count = 0
        self.cached: dict[str, str] = {}

    async def allow(self, key_hash: str) -> bool:
        self.count += 1
        return self.count <= self.limit

    async def get_cached(self, cache_key: str) -> str | None:
        return self.cached.get(cache_key)

    async def put_cached(self, cache_key: str, response_json: str) -> None:
        self.cached[cache_key] = response_json


class Collector:
    def __init__(self) -> None:
        self.events: list[RequestTelemetry] = []
        self.fail = False

    async def record(self, event: RequestTelemetry) -> None:
        if self.fail:
            raise RuntimeError("database unavailable")
        self.events.append(event)


def make_app(provider: Provider, store: Store, collector: Collector):
    return create_app(
        gateway_api_key=KEY,
        providers={"openai": provider},
        model_aliases=ALIASES,
        retry_policy=RetryPolicy(max_retries=0),
        redis_store=store,
        price_book=PRICES,
        telemetry_repository=collector,
    )


def test_success_records_usage_cost_route_and_no_body() -> None:
    provider, store, collector = Provider(), Store(), Collector()
    with TestClient(make_app(provider, store, collector)) as client:
        response = client.post("/v1/chat", json=BODY, headers=HEADERS)
    assert response.status_code == 200
    event = collector.events[0]
    assert str(event.request_id) == response.json()["request_id"] == response.headers["X-Request-ID"]
    assert event.gateway_key_id == hashlib.sha256(KEY.encode()).hexdigest()
    assert (event.model_alias, event.initial_provider, event.final_provider, event.actual_model) == (
        "fast", "openai", "openai", "fixture-model"
    )
    assert (event.input_tokens, event.output_tokens) == (100, 50)
    assert event.estimated_cost_usd == Decimal("0.000060000000")
    assert event.price_version == "fixture-v1"
    assert (event.status_code, event.outcome, event.cache_hit) == (200, "success", False)
    assert event.latency_ms >= 0
    assert response.headers["X-Telemetry-Status"] == "stored"
    assert [(attempt.provider, attempt.failure_reason) for attempt in event.attempts] == [
        ("openai", None)
    ]
    assert "sensitive prompt" not in repr(event)
    assert KEY not in repr(event)
    assert "answer body" not in repr(event)


def test_cache_hit_records_zero_provider_usage_and_cost() -> None:
    provider, store, collector = Provider(), Store(), Collector()
    body = {**BODY, "temperature": 0, "cache": True}
    with TestClient(make_app(provider, store, collector)) as client:
        first = client.post("/v1/chat", json=body, headers=HEADERS)
        second = client.post("/v1/chat", json=body, headers=HEADERS)
    assert first.status_code == second.status_code == 200
    assert provider.calls == 1
    assert [event.outcome for event in collector.events] == ["success", "cache_hit"]
    cached = collector.events[1]
    assert str(cached.request_id) == second.json()["request_id"]
    assert cached.cache_hit
    assert (cached.input_tokens, cached.output_tokens) == (0, 0)
    assert cached.estimated_cost_usd == Decimal("0")
    assert cached.actual_model == "fixture-model"
    assert cached.attempts == ()


def test_rate_limit_and_provider_error_record_outcomes() -> None:
    provider, store, collector = Provider(), Store(limit=1), Collector()
    provider.fail = True
    with TestClient(make_app(provider, store, collector)) as client:
        failed = client.post("/v1/chat", json=BODY, headers=HEADERS)
        limited = client.post("/v1/chat", json=BODY, headers=HEADERS)
    assert (failed.status_code, limited.status_code) == (502, 429)
    upstream, blocked = collector.events
    assert (upstream.outcome, upstream.failure_reason, upstream.status_code) == (
        "error", "server_error", 502
    )
    assert upstream.actual_model == "fixture-model"
    assert upstream.input_tokens is None and upstream.estimated_cost_usd is None
    assert blocked.outcome == "rate_limited" and blocked.status_code == 429
    assert blocked.gateway_key_id == hashlib.sha256(KEY.encode()).hexdigest()
    assert blocked.model_alias == "fast" and blocked.final_provider is None
    assert provider.calls == 1


def test_fallback_records_both_providers_and_final_model_cost() -> None:
    initial, fallback, store, collector = Provider(), Provider(), Store(), Collector()
    initial.fail = True
    app = create_app(
        gateway_api_key=KEY,
        providers={"openai": initial, "anthropic": fallback},
        model_aliases={"fast": ModelAlias("openai", {
            "openai": "first-model", "anthropic": "fallback-model"
        })},
        retry_policy=RetryPolicy(max_retries=0),
        redis_store=store,
        price_book=PriceBook(version="fallback-v1", models={
            ("anthropic", "fallback-model"): TokenPrice(Decimal("1"), Decimal("2"))
        }),
        telemetry_repository=collector,
    )
    with TestClient(app) as client:
        response = client.post("/v1/chat", json=BODY, headers=HEADERS)
    assert response.status_code == 200
    event = collector.events[0]
    assert (event.initial_provider, event.final_provider, event.actual_model) == (
        "openai", "anthropic", "fallback-model"
    )
    assert event.failure_reason == "server_error" and event.retry_count == 0
    assert [attempt.provider for attempt in event.attempts] == ["openai", "anthropic"]
    assert event.estimated_cost_usd == Decimal("0.000200000000")
    assert initial.calls == fallback.calls == 1


def test_invalid_key_and_invalid_body_are_recorded_without_secrets() -> None:
    provider, store, collector = Provider(), Store(), Collector()
    with TestClient(make_app(provider, store, collector)) as client:
        invalid_key = client.post(
            "/v1/chat", json=BODY, headers={"Authorization": "Bearer wrong-secret"}
        )
        invalid_body = client.post("/v1/chat", json={"model": "fast", "messages": []}, headers=HEADERS)
        missing_key_and_bad_body = client.post(
            "/v1/chat", json={"model": "fast", "messages": []}
        )
    assert (invalid_key.status_code, invalid_body.status_code, missing_key_and_bad_body.status_code) == (401, 422, 401)
    unauthorized, malformed, missing_key = collector.events
    assert unauthorized.outcome == "unauthorized" and unauthorized.gateway_key_id is None
    assert malformed.outcome == "invalid_request" and malformed.input_tokens is None
    assert malformed.gateway_key_id == hashlib.sha256(KEY.encode()).hexdigest()
    assert missing_key.outcome == "unauthorized" and missing_key.gateway_key_id is None
    assert all(response.headers["X-Telemetry-Status"] == "stored" for response in (
        invalid_key, invalid_body, missing_key_and_bad_body
    ))
    assert "wrong-secret" not in repr(collector.events)
    assert provider.calls == 0


def test_telemetry_write_failure_is_visible_without_failing_chat(caplog) -> None:
    provider, store, collector = Provider(), Store(), Collector()
    collector.fail = True
    with TestClient(make_app(provider, store, collector)) as client:
        response = client.post("/v1/chat", json=BODY, headers=HEADERS)
    assert response.status_code == 200
    assert response.headers["X-Telemetry-Status"] == "failed"
    assert response.headers["X-Request-ID"] in caplog.text
    assert KEY not in caplog.text and "sensitive prompt" not in caplog.text
    assert provider.calls == 1


def test_disabled_telemetry_is_visible(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    provider = Provider()
    app = create_app(
        gateway_api_key=KEY,
        providers={"openai": provider},
        model_aliases=ALIASES,
        redis_store=Store(),
        price_book=PRICES,
    )
    with TestClient(app) as client:
        response = client.post("/v1/chat", json=BODY, headers=HEADERS)
    assert response.status_code == 200
    assert response.headers["X-Telemetry-Status"] == "disabled"


def test_price_config_loads_version_and_rejects_bad_rates(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "prices.json"
    path.write_text(json.dumps({
        "version": "test-v2",
        "models": {"openai": {"model": {
            "input_per_million_usd": "0.50",
            "output_per_million_usd": "1.00",
        }}},
    }))
    monkeypatch.setenv("GATEWAY_PRICE_CONFIG_PATH", str(path))
    book = PriceBook.from_env()
    assert book.version == "test-v2"
    assert book.estimate("openai", "model", 10, 5) == Decimal("0.000010000000")
    assert book.estimate("openai", "unknown", 10, 5) is None
    path.write_text(json.dumps({
        "version": "bad",
        "models": {"openai": {"model": {
            "input_per_million_usd": "-1",
            "output_per_million_usd": "1",
        }}},
    }))
    with pytest.raises(ValueError, match="Invalid GATEWAY_PRICE_CONFIG_PATH"):
        PriceBook.from_env()


@pytest.mark.parametrize("filename", ["prices.json", "prices.example.json"])
def test_local_fixture_prices_allow_api_startup(filename: str, monkeypatch: pytest.MonkeyPatch) -> None:
    path = Path(__file__).resolve().parents[1] / filename
    if not path.exists():
        pytest.skip("Local prices.json has not been created")
    monkeypatch.setenv("GATEWAY_PRICE_CONFIG_PATH", str(path))
    book = PriceBook.from_env()
    assert book.models
    if filename == "prices.example.json":
        assert book.version == "local-test-pricing-v1"
        assert book.estimate("openai", "your-fast-openai-model", 100, 50) == Decimal("0.000125000000")
    with TestClient(create_app(
        gateway_api_key=KEY,
        providers={}, model_aliases={}, redis_store=Store(),
        telemetry_repository=Collector(),
    )) as client:
        assert client.get("/health").json() == {"status": "ok"}


def test_postgres_repository_uses_parameterized_metadata_only() -> None:
    event = RequestTelemetry(
        request_id=uuid4(),
        price_version="test-v1",
        gateway_key_id="a" * 64,
        model_alias="fast",
        attempts=(Attempt("openai", "fixture-model", None),),
    )
    captured: list[tuple[str, tuple[object, ...]]] = []

    class FakeConnection:
        async def execute(self, sql: str, params: tuple[object, ...]) -> None:
            captured.append((sql, params))

    class FakePool:
        @asynccontextmanager
        async def connection(self):
            yield FakeConnection()

    import asyncio

    async def run() -> None:
        repository = PostgresTelemetryRepository("postgresql://unused")
        repository.pool = FakePool()
        await repository.record(event)

    asyncio.run(run())
    sql, params = captured[0]
    assert sql == INSERT_SQL and len(params) == len(INSERT_COLUMNS)
    assert all(column not in INSERT_COLUMNS for column in ("messages", "prompt", "response_body"))
    assert isinstance(params[-1], Jsonb)
    assert params[-1].obj == [{
        "provider": "openai", "model": "fixture-model", "failure_reason": None
    }]
