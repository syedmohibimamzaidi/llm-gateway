import hashlib
from datetime import datetime, timezone
from uuid import uuid4

from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.config import ModelAlias
from gateway.keys import KeyRecord
from gateway.pricing import PriceBook
from gateway.providers.base import Generation, Usage
from gateway.routing import RetryPolicy

BODY = {"model": "fast", "messages": [{"role": "user", "content": "Hi"}],
        "temperature": 0, "cache": True}


class MemoryStore:
    def __init__(self, limit=60):
        self.limit = limit
        self.counts = {}
        self.cached = {}

    async def allow(self, key_hash):
        self.counts[key_hash] = self.counts.get(key_hash, 0) + 1
        return self.counts[key_hash] <= self.limit

    async def get_cached(self, cache_key):
        return self.cached.get(cache_key)

    async def put_cached(self, cache_key, response_json):
        self.cached[cache_key] = response_json


ADMIN = "configured-admin-key"
ADMIN_HEADERS = {"Authorization": f"Bearer {ADMIN}"}


class MemoryKeys:
    def __init__(self):
        self.records = {}

    async def is_active(self, key_hash):
        return key_hash in self.records and self.records[key_hash][0].revoked_at is None

    async def create(self, key_id, key_hash, name):
        record = KeyRecord(key_id, name, datetime.now(timezone.utc), None)
        self.records[key_hash] = (record, key_hash)
        return record

    async def list(self):
        return [record for record, _ in self.records.values()]

    async def revoke(self, key_id):
        for key_hash, (record, _) in self.records.items():
            if record.id == key_id:
                updated = KeyRecord(record.id, record.name, record.created_at,
                                    record.revoked_at or datetime.now(timezone.utc))
                self.records[key_hash] = (updated, key_hash)
                return updated
        return None


class FakeProvider:
    name = "openai"

    def __init__(self):
        self.calls = 0

    async def generate(self, messages, *, model, options):
        self.calls += 1
        return Generation("fixture", model, "stop", Usage(2, 1))


class NoopTelemetry:
    async def record(self, event):
        pass


class FakeAnalytics:
    def __init__(self):
        self.seen_key_ids = []

    async def summary(self, key_id, start, end):
        self.seen_key_ids.append(key_id)
        return {"request_count": 0}


def make_app(keys, store, provider, analytics):
    return create_app(
        gateway_api_key=ADMIN,
        providers={"openai": provider},
        model_aliases={"fast": ModelAlias("openai", {"openai": "fixture-model"})},
        retry_policy=RetryPolicy(max_retries=0),
        redis_store=store,
        price_book=PriceBook("test", {}),
        telemetry_repository=NoopTelemetry(),
        analytics_repository=analytics,
        key_repository=keys,
    )


def test_key_lifecycle_and_per_key_cache_rate_limit_and_analytics() -> None:
    keys, store, provider, analytics = MemoryKeys(), MemoryStore(limit=2), FakeProvider(), FakeAnalytics()
    with TestClient(make_app(keys, store, provider, analytics)) as client:
        assert client.post("/api-keys", json={"name": "x"}).status_code == 401
        assert client.post("/api-keys", json={"name": "x"}, headers={
            "Authorization": "Bearer incorrect"
        }).status_code == 401
        created = client.post("/api-keys", json={"name": "Client A"}, headers=ADMIN_HEADERS)
        assert created.status_code == 201
        assert created.headers["Cache-Control"] == "no-store"
        secret = created.json()["key"]
        key_id = created.json()["id"]
        secret_hash = hashlib.sha256(secret.encode()).hexdigest()
        assert secret.startswith("gw_") and secret_hash in keys.records
        assert secret not in str(keys.records)
        managed_headers = {"Authorization": f"Bearer {secret}"}
        listing = client.get("/api-keys", headers=ADMIN_HEADERS)
        assert listing.status_code == 200
        assert listing.json()[0]["id"] == key_id
        assert secret not in listing.text and secret_hash not in listing.text
        assert client.get("/api-keys", headers=managed_headers).status_code == 403
        assert client.post("/api-keys", json={"name": "Another"}, headers=managed_headers).status_code == 403
        assert client.delete(f"/api-keys/{key_id}", headers=managed_headers).status_code == 403

        first = client.post("/v1/chat", json=BODY, headers=managed_headers)
        second = client.post("/v1/chat", json=BODY, headers=managed_headers)
        admin_chat = client.post("/v1/chat", json=BODY, headers=ADMIN_HEADERS)
        assert [first.status_code, second.status_code, admin_chat.status_code] == [200, 200, 200]
        assert [first.json()["cache_hit"], second.json()["cache_hit"], admin_chat.json()["cache_hit"]] == [False, True, False]
        assert provider.calls == 2
        assert store.counts[secret_hash] == 2
        assert store.counts[hashlib.sha256(ADMIN.encode()).hexdigest()] == 1
        managed_summary = client.get("/analytics/summary", headers=managed_headers)
        assert managed_summary.status_code == 200
        assert analytics.seen_key_ids == [secret_hash]
        assert secret not in managed_summary.text and secret_hash not in managed_summary.text

        revoked = client.delete(f"/api-keys/{key_id}", headers=ADMIN_HEADERS)
        assert revoked.status_code == 200 and revoked.json()["revoked_at"] is not None
        assert "key" not in revoked.json()
        assert client.delete(f"/api-keys/{key_id}", headers=ADMIN_HEADERS).json()["revoked_at"] == revoked.json()["revoked_at"]
        assert client.delete(f"/api-keys/{uuid4()}", headers=ADMIN_HEADERS).status_code == 404
        assert client.post("/v1/chat", json=BODY, headers=managed_headers).status_code == 401
        malformed = {"model": "fast", "messages": []}
        assert client.post("/v1/chat", json=malformed, headers=managed_headers).status_code == 401
        assert client.post("/v1/chat", json=malformed, headers={
            "Authorization": "Bearer gw_invalid"
        }).status_code == 401
        assert client.get("/analytics/summary", headers=managed_headers).status_code == 401
        assert client.post("/v1/chat", json=BODY, headers=ADMIN_HEADERS).status_code == 200
        assert provider.calls == 2


def test_key_validation_and_unavailable_repository(caplog) -> None:
    keys, store, provider, analytics = MemoryKeys(), MemoryStore(), FakeProvider(), FakeAnalytics()
    with TestClient(make_app(keys, store, provider, analytics)) as client:
        for bad_name in ("", "x" * 101):
            assert client.post("/api-keys", json={"name": bad_name}, headers=ADMIN_HEADERS).status_code == 422
        assert client.delete("/api-keys/not-a-uuid", headers=ADMIN_HEADERS).status_code == 422

    app = create_app(gateway_api_key=ADMIN, providers={}, model_aliases={},
                     redis_store=store, price_book=PriceBook("test", {}),
                     telemetry_repository=NoopTelemetry())
    with TestClient(app) as client:
        assert client.get("/api-keys", headers=ADMIN_HEADERS).status_code == 503

    class BrokenKeys(MemoryKeys):
        async def is_active(self, key_hash):
            raise RuntimeError("sensitive exception detail")

        async def create(self, key_id, key_hash, name):
            raise RuntimeError("sensitive exception detail")

    with TestClient(make_app(BrokenKeys(), store, provider, analytics)) as client:
        assert client.post("/api-keys", json={"name": "test"}, headers=ADMIN_HEADERS).status_code == 503
        assert client.get("/analytics/summary", headers={"Authorization": "Bearer gw_invalid"}).status_code == 503
        assert client.get("/analytics/summary", headers=ADMIN_HEADERS).status_code == 200
    assert "sensitive exception detail" not in caplog.text
    assert ADMIN not in caplog.text
