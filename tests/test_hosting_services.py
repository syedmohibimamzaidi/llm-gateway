"""Isolated admission/migration/readiness fixtures; never call live providers."""

import asyncio
from pathlib import Path
import os
import time
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
import pytest
import redis.asyncio as redis
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.config import ModelAlias
from gateway.migrate import MIGRATIONS, migrate
from gateway.pricing import PriceBook
from gateway.redis_store import RedisPolicy, RedisStore
from gateway.safety import RedisSafetyStore, SafetyPolicy
from gateway.telemetry import PostgresTelemetryRepository


DATABASE_URL = os.getenv("TEST_DATABASE_URL")
REDIS_URL = os.getenv("TEST_REDIS_URL")


@pytest.mark.skipif(not REDIS_URL, reason="Set TEST_REDIS_URL for distributed admission")
def test_real_redis_admission_is_atomic_shared_and_expiring():
    async def run():
        namespace = "hosting-fixture:" + uuid4().hex
        first = redis.from_url(REDIS_URL, decode_responses=True)
        second = redis.from_url(REDIS_URL, decode_responses=True)
        policy = SafetyPolicy(concurrency=3, key_concurrency=1, ingress_limit=3, peer_limit=2)
        a, b = RedisSafetyStore(first, policy, namespace), RedisSafetyStore(second, policy, namespace)
        try:
            assert await a.allow_ingress("peer-a")
            assert await b.allow_ingress("peer-a")
            assert not await a.allow_ingress("peer-a")
            assert not await b.allow_ingress("peer-b")  # Shared global bound, including rejections.
            assert 0 < await first.ttl(namespace + ":ingress") <= 60
            lease = await a.acquire("same-key", 1)
            assert lease and await b.acquire("same-key", 1) is None
            await b.release("same-key", lease)
            leases = await asyncio.gather(*( (a if i % 2 else b).acquire(str(i), 1) for i in range(20)))
            assert sum(value is not None for value in leases) == 3
            for i, token in enumerate(leases):
                if token:
                    await a.release(str(i), token)
            abandoned = await a.acquire("abandoned", .05)
            assert abandoned
            await asyncio.sleep(.08)
            recovered = await b.acquire("abandoned", 1)
            assert recovered
            await a.release("abandoned", recovered)
            assert await first.zcard(namespace + ":active") == 0
            # A shorter deadline in a rolling deploy must not shorten an older lease's key TTL.
            long_lease = await a.acquire("long", .4)
            short_lease = await b.acquire("short", .05)
            assert long_lease and short_lease
            await asyncio.sleep(.08)
            assert await first.zcard(namespace + ":active") >= 1
            assert await b.acquire("long", 1) is None
            await a.release("long", long_lease)
            await b.release("short", short_lease)
        finally:
            keys = [key async for key in first.scan_iter(match=namespace + ":*")]
            if keys:
                await first.delete(*keys)
            await first.aclose()
            await second.aclose()
    asyncio.run(run())


@pytest.mark.skipif(not DATABASE_URL, reason="Set TEST_DATABASE_URL for migrations")
def test_migrations_replay_in_isolated_schema_and_failed_transaction_rolls_back(tmp_path):
    schema = "hosting_fixture_" + uuid4().hex
    with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    scoped = make_conninfo(DATABASE_URL, options="-csearch_path=" + schema)
    try:
        migrate(scoped)
        with psycopg.connect(scoped) as connection:
            request_id = uuid4()
            connection.execute("INSERT INTO request_telemetry (request_id, latency_ms, status_code, outcome, price_version) VALUES (%s, 1, 200, 'success', 'fixture')", (request_id,))
        migrate(scoped)
        with psycopg.connect(scoped) as connection:
            assert connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 2
            assert connection.execute("SELECT request_id FROM request_telemetry").fetchone()[0] == request_id
        (tmp_path / "999_failure.sql").write_text("BEGIN; CREATE TABLE should_rollback (id int); SELECT nonexistent_column; COMMIT;")
        with pytest.raises(psycopg.Error):
            migrate(scoped, tmp_path)
        with psycopg.connect(scoped) as connection:
            assert connection.execute("SELECT to_regclass('should_rollback')").fetchone()[0] is None
            assert connection.execute("SELECT count(*) FROM request_telemetry").fetchone()[0] == 1
    finally:
        with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.mark.skipif(not DATABASE_URL or not REDIS_URL, reason="Set both test URLs for readiness")
def test_readiness_requires_redis_and_current_schema_without_provider_calls(monkeypatch):
    class NeverProvider:
        name = "openai"
        async def generate(self, *args, **kwargs):
            raise AssertionError("Readiness must never call a provider")
    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    repository = PostgresTelemetryRepository(DATABASE_URL)
    app = create_app(gateway_api_key="readiness-fixture", providers={"openai": NeverProvider()}, model_aliases={"fast": ModelAlias("openai", {"openai": "fixture"})}, telemetry_repository=repository, price_book=PriceBook("fixture", {}))
    with TestClient(app) as http:
        assert http.get("/health").status_code == 200
        assert http.get("/readyz").json() == {"status": "ready"}
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    down_app = create_app(gateway_api_key="readiness-fixture", providers={}, model_aliases={}, telemetry_repository=PostgresTelemetryRepository(DATABASE_URL), price_book=PriceBook("fixture", {}))
    with TestClient(down_app) as http:
        assert http.get("/health").status_code == 200
        response = http.get("/readyz")
        assert response.status_code == 503 and response.json() == {"status": "not_ready"}


@pytest.mark.skipif(not DATABASE_URL or not REDIS_URL, reason="Set both test URLs for schema readiness")
def test_readiness_rejects_missing_schema_then_accepts_migrated_schema(monkeypatch):
    schema = "ready_fixture_" + uuid4().hex
    with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    scoped = make_conninfo(DATABASE_URL, options="-csearch_path=" + schema)
    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    def application():
        return create_app(gateway_api_key="readiness-fixture", providers={}, model_aliases={},
                          telemetry_repository=PostgresTelemetryRepository(scoped), price_book=PriceBook("fixture", {}))
    try:
        with TestClient(application()) as http:
            assert http.get("/readyz").status_code == 503
        migrate(scoped)
        with TestClient(application()) as http:
            assert http.get("/readyz").status_code == 200
    finally:
        with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
