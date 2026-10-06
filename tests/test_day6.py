import hashlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi.testclient import TestClient

from gateway.analytics import present_metrics, window_bounds
from gateway.app import create_app
from gateway.pricing import PriceBook


KEY = "analytics-fixture-secret"
KEY_ID = hashlib.sha256(KEY.encode()).hexdigest()
HEADERS = {"Authorization": f"Bearer {KEY}"}


class NoopTelemetry:
    async def record(self, event):
        pass


class FakeAnalytics:
    def __init__(self):
        self.calls = []
        self.fail = False

    async def summary(self, key_id, start, end):
        self.calls.append(("summary", key_id, start, end))
        if self.fail:
            raise RuntimeError("database unavailable")
        return present_metrics({
            "request_count": 4, "success_count": 3, "cache_hit_count": 1,
            "error_count": 1, "input_tokens": 120, "output_tokens": 30,
            "estimated_cost_usd": Decimal("0.000105000000"),
            "unpriced_success_count": 0,
            "mean_latency_ms": 25.0, "p95_latency_ms": 77.5,
        })

    async def providers(self, key_id, start, end):
        self.calls.append(("providers", key_id, start, end))
        return [{"provider": "openai", **await self.summary(key_id, start, end)}]

    async def requests(self, key_id, start, end, limit, offset):
        self.calls.append(("requests", key_id, start, end, limit, offset))
        return 1, [{
            "request_id": "12345678-1234-1234-1234-123456789abc",
            "model_alias": "fast", "final_provider": "openai", "outcome": "success",
            "input_tokens": 120, "output_tokens": 30, "estimated_cost_usd": "0.000105000000",
        }]


def app_with(reader):
    return create_app(
        gateway_api_key=KEY,
        providers={},
        model_aliases={},
        redis_store=object(),
        price_book=PriceBook("test", {}),
        telemetry_repository=NoopTelemetry(),
        analytics_repository=reader,
    )


def test_analytics_requires_gateway_key_and_never_returns_identifiers() -> None:
    reader = FakeAnalytics()
    with TestClient(app_with(reader)) as client:
        assert client.get("/analytics/summary").status_code == 401
        assert client.get("/analytics/providers", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.get("/requests").status_code == 401
        summary = client.get("/analytics/summary?window=7d", headers=HEADERS)
        providers = client.get("/analytics/providers?window=7d", headers=HEADERS)
        history = client.get("/requests?window=7d&limit=10&offset=5", headers=HEADERS)
    assert summary.status_code == providers.status_code == history.status_code == 200
    assert summary.headers["Cache-Control"] == "no-store"
    assert summary.json()["request_count"] == 4
    assert summary.json()["success_rate"] == 0.75
    assert summary.json()["cache_hit_rate"] == 0.25
    assert summary.json()["estimated_cost_usd"] == "0.000105000000"
    assert providers.json()["providers"][0]["provider"] == "openai"
    assert history.json()["total"] == 1
    assert (history.json()["limit"], history.json()["offset"]) == (10, 5)
    assert all(call[1] == KEY_ID for call in reader.calls)
    for response in (summary, providers, history):
        assert KEY not in response.text and KEY_ID not in response.text
        assert "gateway_key_id" not in response.text
        assert "messages" not in response.text and "response_text" not in response.text


def test_range_validation_pagination_and_unavailable_database() -> None:
    reader = FakeAnalytics()
    with TestClient(app_with(reader)) as client:
        assert client.get("/analytics/summary?window=year", headers=HEADERS).status_code == 422
        assert client.get("/requests?limit=101", headers=HEADERS).status_code == 422
        assert client.get("/requests?offset=-1", headers=HEADERS).status_code == 422
        reader.fail = True
        unavailable = client.get("/analytics/summary", headers=HEADERS)
    assert unavailable.status_code == 503
    assert unavailable.json() == {"detail": "Analytics unavailable"}
    assert len(reader.calls) == 1
    with TestClient(app_with(None)) as client:
        assert client.get("/analytics/summary", headers=HEADERS).status_code == 503


def test_empty_metrics_and_window_bounds() -> None:
    empty = present_metrics({
        "request_count": 0, "success_count": 0, "cache_hit_count": 0,
        "error_count": 0, "input_tokens": 0, "output_tokens": 0,
        "estimated_cost_usd": Decimal("0"), "unpriced_success_count": 0,
        "mean_latency_ms": 0.0, "p95_latency_ms": 0.0,
    })
    assert (empty["success_rate"], empty["cache_hit_rate"], empty["error_rate"]) == (0, 0, 0)
    now = datetime(2026, 9, 28, tzinfo=timezone.utc)
    assert window_bounds("24h", now) == (now - timedelta(hours=24), now)
