"""Run with TEST_DATABASE_URL set to a migrated PostgreSQL database."""

import hashlib
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.pricing import PriceBook
from gateway.telemetry import PostgresTelemetryRepository


DSN = os.getenv("TEST_DATABASE_URL")


@pytest.mark.skipif(not DSN, reason="Set TEST_DATABASE_URL for the PostgreSQL integration check")
def test_postgres_aggregates_match_known_rows_and_scope() -> None:
    key = "analytics-integration-" + uuid4().hex
    key_id = hashlib.sha256(key.encode()).hexdigest()
    other_key_id = hashlib.sha256(uuid4().hex.encode()).hexdigest()
    now = datetime.now(timezone.utc)
    rows = [
        (uuid4(), key_id, "fast", "openai", "openai", "o-fixture", 100, 20,
         100.0, 200, "success", False, Decimal("0.000080000000"), "fixture", now - timedelta(minutes=5)),
        (uuid4(), key_id, "fast", "openai", "openai", "o-fixture", 0, 0,
         10.0, 200, "cache_hit", True, Decimal("0"), "fixture", now - timedelta(minutes=10)),
        (uuid4(), key_id, "smart", "anthropic", "anthropic", "a-fixture", None, None,
         500.0, 502, "error", False, None, "fixture", now - timedelta(minutes=15)),
        (uuid4(), key_id, "smart", "anthropic", "anthropic", "a-fixture", 200, 50,
         200.0, 200, "success", False, Decimal("0.000400000000"), "fixture", now - timedelta(minutes=20)),
        (uuid4(), key_id, "old", "openai", "openai", "o-fixture", 900, 900,
         900.0, 200, "success", False, Decimal("9"), "fixture", now - timedelta(hours=25)),
        (uuid4(), other_key_id, "other", "openai", "openai", "o-fixture", 700, 700,
         700.0, 200, "success", False, Decimal("7"), "fixture", now - timedelta(minutes=1)),
    ]
    insert = """
        INSERT INTO request_telemetry (
            request_id, gateway_key_id, model_alias, initial_provider, final_provider,
            actual_model, input_tokens, output_tokens, latency_ms, status_code,
            outcome, cache_hit, estimated_cost_usd, price_version, created_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
    ids = [row[0] for row in rows]
    inserted = False
    try:
        with psycopg.connect(DSN) as connection:
            with connection.cursor() as cursor:
                cursor.executemany(insert, rows)
        inserted = True
        app = create_app(
            gateway_api_key=key,
            providers={}, model_aliases={}, redis_store=object(),
            price_book=PriceBook("fixture", {}),
            telemetry_repository=PostgresTelemetryRepository(DSN),
        )
        headers = {"Authorization": f"Bearer {key}"}
        with TestClient(app) as client:
            summary_response = client.get("/analytics/summary?window=24h", headers=headers)
            providers_response = client.get("/analytics/providers?window=24h", headers=headers)
            history_response = client.get("/requests?window=24h&limit=2&offset=0", headers=headers)
            older_response = client.get("/analytics/summary?window=30d", headers=headers)
        assert all(response.status_code == 200 for response in (
            summary_response, providers_response, history_response, older_response
        ))
        summary = summary_response.json()
        assert (summary["request_count"], summary["success_count"], summary["cache_hit_count"],
                summary["error_count"]) == (4, 3, 1, 1)
        assert (summary["success_rate"], summary["cache_hit_rate"], summary["error_rate"]) == (
            0.75, 0.25, 0.25
        )
        assert (summary["input_tokens"], summary["output_tokens"]) == (300, 70)
        assert Decimal(summary["estimated_cost_usd"]) == Decimal("0.000480000000")
        assert summary["mean_latency_ms"] == 202.5
        assert summary["p95_latency_ms"] == 455.0
        assert summary["unpriced_success_count"] == 0
        assert older_response.json()["request_count"] == 5

        providers = {item["provider"]: item for item in providers_response.json()["providers"]}
        assert set(providers) == {"openai", "anthropic"}
        assert providers["openai"]["request_count"] == 2
        assert providers["openai"]["cache_hit_count"] == 1
        assert (providers["openai"]["input_tokens"], providers["openai"]["output_tokens"]) == (100, 20)
        assert Decimal(providers["openai"]["estimated_cost_usd"]) == Decimal("0.000080000000")
        assert providers["anthropic"]["request_count"] == 2
        assert providers["anthropic"]["error_count"] == 1
        assert (providers["anthropic"]["input_tokens"], providers["anthropic"]["output_tokens"]) == (200, 50)
        assert Decimal(providers["anthropic"]["estimated_cost_usd"]) == Decimal("0.000400000000")

        history = history_response.json()
        assert history["total"] == 4 and len(history["items"]) == 2
        assert history["items"][0]["request_id"] == str(rows[0][0])
        assert history["items"][1]["request_id"] == str(rows[1][0])
        for response in (summary_response, providers_response, history_response):
            assert key not in response.text and key_id not in response.text
            assert "gateway_key_id" not in response.text
            assert "messages" not in response.text and "response_text" not in response.text
    finally:
        if inserted:
            with psycopg.connect(DSN) as connection:
                with connection.cursor() as cursor:
                    cursor.execute("DELETE FROM request_telemetry WHERE request_id = ANY(%s)", (ids,))


@pytest.mark.skipif(not DSN, reason="Set TEST_DATABASE_URL for the PostgreSQL integration check")
def test_malformed_authenticated_chat_is_visible_to_its_key() -> None:
    key = "validation-integration-" + uuid4().hex
    key_id = hashlib.sha256(key.encode()).hexdigest()
    request_ids = []
    app = create_app(
        gateway_api_key=key,
        providers={}, model_aliases={}, redis_store=object(),
        price_book=PriceBook("validation-fixture", {}),
        telemetry_repository=PostgresTelemetryRepository(DSN),
    )
    try:
        with TestClient(app) as client:
            malformed = client.post(
                "/v1/chat", json={"model": "fast", "messages": []},
                headers={"Authorization": f"Bearer {key}"},
            )
            request_ids.append(UUID(malformed.headers["X-Request-ID"]))
            assert malformed.status_code == 422
            assert malformed.headers["X-Telemetry-Status"] == "stored"
            summary = client.get(
                "/analytics/summary", headers={"Authorization": f"Bearer {key}"}
            )
            history = client.get("/requests", headers={"Authorization": f"Bearer {key}"})
            assert summary.status_code == history.status_code == 200
            assert summary.json()["request_count"] == 1
            assert summary.json()["error_count"] == 1
            assert history.json()["items"][0]["request_id"] == str(request_ids[0])
            assert history.json()["items"][0]["outcome"] == "invalid_request"
        with psycopg.connect(DSN) as connection:
            row = connection.execute(
                "SELECT gateway_key_id FROM request_telemetry WHERE request_id = %s",
                (request_ids[0],),
            ).fetchone()
        assert row == (key_id,)
    finally:
        if request_ids:
            with psycopg.connect(DSN) as connection:
                connection.execute(
                    "DELETE FROM request_telemetry WHERE request_id = ANY(%s)",
                    (request_ids,),
                )
