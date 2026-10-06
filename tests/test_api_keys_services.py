"""Managed-key lifecycle with real Redis/PostgreSQL and a fake provider."""

import hashlib
import os
import time
from uuid import UUID, uuid4

import psycopg
import pytest
import redis
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.config import ModelAlias
from gateway.pricing import PriceBook
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
        return Generation("fixture response", model, "stop", Usage(7, 3))


@pytest.mark.skipif(
    not DATABASE_URL or not REDIS_URL,
    reason="Set TEST_DATABASE_URL and TEST_REDIS_URL for the live services check",
)
def test_managed_key_create_auth_revoke_and_private_storage(monkeypatch):
    admin = "keys-admin-fixture-" + uuid4().hex
    admin_hash = hashlib.sha256(admin.encode()).hexdigest()
    admin_headers = {"Authorization": "Bearer " + admin}
    prompt = "keys-prompt-" + uuid4().hex
    body = {"model": "fast", "messages": [{"role": "user", "content": prompt}],
            "temperature": 0, "cache": True}
    provider = FakeProvider()
    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    monkeypatch.setenv("GATEWAY_RATE_LIMIT", "3")
    app = create_app(
        gateway_api_key=admin,
        providers={"openai": provider},
        model_aliases={"fast": ModelAlias("openai", {"openai": "fixture-model"})},
        retry_policy=RetryPolicy(max_retries=0),
        price_book=PriceBook("keys-test-pricing", {}),
        telemetry_repository=PostgresTelemetryRepository(DATABASE_URL),
    )
    managed_id = None
    managed_hash = None
    request_ids = []
    try:
        with TestClient(app) as client:
            created = client.post("/api-keys", json={"name": "integration fixture"}, headers=admin_headers)
            assert created.status_code == 201
            secret = created.json()["key"]
            managed_id = UUID(created.json()["id"])
            managed_hash = hashlib.sha256(secret.encode()).hexdigest()
            managed_headers = {"Authorization": "Bearer " + secret}

            with psycopg.connect(DATABASE_URL) as connection:
                stored = connection.execute(
                    "SELECT key_hash, name, revoked_at FROM gateway_keys WHERE id = %s",
                    (managed_id,),
                ).fetchone()
            assert stored == (managed_hash, "integration fixture", None)
            assert secret not in str(stored)

            listing = client.get("/api-keys", headers=admin_headers)
            assert listing.status_code == 200
            assert any(row["id"] == str(managed_id) for row in listing.json())
            assert secret not in listing.text and managed_hash not in listing.text
            assert client.get("/api-keys", headers=managed_headers).status_code == 403
            assert client.post("/api-keys", json={"name": "forbidden"}, headers=managed_headers).status_code == 403
            assert client.delete(f"/api-keys/{managed_id}", headers=managed_headers).status_code == 403

            first = client.post("/v1/chat", json=body, headers=managed_headers)
            request_ids.append(UUID(first.headers["X-Request-ID"]))
            second = client.post("/v1/chat", json=body, headers=managed_headers)
            request_ids.append(UUID(second.headers["X-Request-ID"]))
            assert first.status_code == second.status_code == 200
            assert not first.json()["cache_hit"] and second.json()["cache_hit"]
            assert provider.calls == 1
            summary = client.get("/analytics/summary", headers=managed_headers)
            own_requests = client.get("/requests", headers=managed_headers)
            admin_summary = client.get("/analytics/summary", headers=admin_headers)
            assert summary.status_code == own_requests.status_code == admin_summary.status_code == 200
            assert summary.json()["request_count"] == 2
            assert summary.json()["cache_hit_count"] == 1
            assert own_requests.json()["total"] == 2
            assert admin_summary.json()["request_count"] == 0
            for response in (summary, own_requests, admin_summary):
                assert secret not in response.text and managed_hash not in response.text

            with psycopg.connect(DATABASE_URL) as connection:
                rows = connection.execute(
                    "SELECT gateway_key_id, input_tokens, output_tokens, cache_hit, "
                    "to_jsonb(request_telemetry)::text FROM request_telemetry WHERE request_id = ANY(%s) "
                    "ORDER BY created_at", (request_ids,),
                ).fetchall()
            assert len(rows) == 2
            assert [(row[0], row[1], row[2], row[3]) for row in rows] == [
                (managed_hash, 7, 3, False), (managed_hash, 0, 0, True)
            ]
            assert all(secret not in row[4] and prompt not in row[4] and admin not in row[4] for row in rows)

            revoked = client.delete(f"/api-keys/{managed_id}", headers=admin_headers)
            assert revoked.status_code == 200 and revoked.json()["revoked_at"] is not None
            assert secret not in revoked.text and managed_hash not in revoked.text
            denied = client.post("/v1/chat", json=body, headers=managed_headers)
            request_ids.append(UUID(denied.headers["X-Request-ID"]))
            assert denied.status_code == 401
            assert client.get("/analytics/summary", headers=managed_headers).status_code == 401
            assert client.get("/api-keys", headers=admin_headers).status_code == 200
    finally:
        with psycopg.connect(DATABASE_URL) as connection:
            if request_ids:
                connection.execute("DELETE FROM request_telemetry WHERE request_id = ANY(%s)", (request_ids,))
            if managed_id:
                connection.execute("DELETE FROM gateway_keys WHERE id = %s", (managed_id,))
        cache_key = make_cache_key(
            key_hash=managed_hash or "", provider="openai", model="fixture-model",
            messages=[Message("user", prompt)], options=GenerationOptions(temperature=0),
        )
        with redis.Redis.from_url(REDIS_URL) as client:
            client.delete("gateway:rate:v1:" + admin_hash,
                          "gateway:rate:v1:" + (managed_hash or ""), cache_key)


@pytest.mark.skipif(
    not DATABASE_URL or not REDIS_URL,
    reason="Set TEST_DATABASE_URL and TEST_REDIS_URL for the live services check",
)
def test_real_services_isolate_keys_and_expire_cache(monkeypatch):
    admin = "isolation-admin-" + uuid4().hex
    admin_hash = hashlib.sha256(admin.encode()).hexdigest()
    admin_headers = {"Authorization": "Bearer " + admin}
    prompt = "isolation-prompt-" + uuid4().hex
    body = {"model": "fast", "messages": [{"role": "user", "content": prompt}],
            "temperature": 0, "cache": True}
    provider = FakeProvider()
    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    monkeypatch.setenv("GATEWAY_RATE_LIMIT", "3")
    monkeypatch.setenv("GATEWAY_CACHE_TTL_SECONDS", "1")
    app = create_app(
        gateway_api_key=admin,
        providers={"openai": provider},
        model_aliases={"fast": ModelAlias("openai", {"openai": "fixture-model"})},
        retry_policy=RetryPolicy(max_retries=0),
        price_book=PriceBook("isolation-test-pricing", {}),
        telemetry_repository=PostgresTelemetryRepository(DATABASE_URL),
    )
    managed_id = None
    managed_hash = None
    request_ids = []
    try:
        with TestClient(app) as client:
            created = client.post("/api-keys", json={"name": "isolation fixture"}, headers=admin_headers)
            assert created.status_code == 201
            managed_id = UUID(created.json()["id"])
            managed_key = created.json()["key"]
            managed_hash = hashlib.sha256(managed_key.encode()).hexdigest()
            managed_headers = {"Authorization": "Bearer " + managed_key}

            def chat(headers):
                response = client.post("/v1/chat", json=body, headers=headers)
                request_ids.append(UUID(response.headers["X-Request-ID"]))
                assert response.headers["X-Telemetry-Status"] == "stored"
                return response

            managed_first = chat(managed_headers)
            admin_first = chat(admin_headers)
            managed_second = chat(managed_headers)
            managed_third = chat(managed_headers)
            managed_limited = chat(managed_headers)
            admin_second = chat(admin_headers)
            assert [response.status_code for response in (
                managed_first, admin_first, managed_second, managed_third,
                managed_limited, admin_second,
            )] == [200, 200, 200, 200, 429, 200]
            assert [response.json()["cache_hit"] for response in (
                managed_first, admin_first, managed_second, managed_third, admin_second,
            )] == [False, False, True, True, True]
            assert provider.calls == 2

            time.sleep(1.2)
            admin_after_expiry = chat(admin_headers)
            assert admin_after_expiry.status_code == 200
            assert not admin_after_expiry.json()["cache_hit"]
            assert provider.calls == 3

            managed_summary = client.get("/analytics/summary", headers=managed_headers)
            admin_summary = client.get("/analytics/summary", headers=admin_headers)
            assert managed_summary.status_code == admin_summary.status_code == 200
            assert (managed_summary.json()["request_count"], managed_summary.json()["cache_hit_count"]) == (4, 2)
            assert (admin_summary.json()["request_count"], admin_summary.json()["cache_hit_count"]) == (3, 1)
    finally:
        with psycopg.connect(DATABASE_URL) as connection:
            if request_ids:
                connection.execute("DELETE FROM request_telemetry WHERE request_id = ANY(%s)", (request_ids,))
            if managed_id:
                connection.execute("DELETE FROM gateway_keys WHERE id = %s", (managed_id,))
        with redis.Redis.from_url(REDIS_URL) as client:
            cache_keys = [make_cache_key(
                key_hash=key_hash, provider="openai", model="fixture-model",
                messages=[Message("user", prompt)], options=GenerationOptions(temperature=0),
            ) for key_hash in (admin_hash, managed_hash) if key_hash]
            client.delete("gateway:rate:v1:" + admin_hash,
                          "gateway:rate:v1:" + (managed_hash or ""), *cache_keys)
