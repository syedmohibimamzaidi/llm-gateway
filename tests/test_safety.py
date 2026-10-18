import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError

from gateway.app import create_app
from gateway.config import ModelAlias
from gateway.keys import KeyRecord
from gateway.pricing import PriceBook
from gateway.providers.base import Generation, ProviderError, Usage
from gateway.routing import RetryPolicy
from gateway.safety import MemorySafetyStore, SafetyPolicy


KEY = "safety-fixture-key"
BODY = {"model": "fast", "messages": [{"role": "user", "content": "fixture"}]}
HEADERS = {"Authorization": "Bearer " + KEY}


class Store:
    def __init__(self):
        self.calls = 0

    async def allow(self, key):
        self.calls += 1
        return True

    async def get_cached(self, key):
        return None

    async def put_cached(self, key, value):
        pass


class Telemetry:
    def __init__(self):
        self.events = []

    async def record(self, event):
        self.events.append(event)


class Provider:
    name = "openai"

    def __init__(self):
        self.calls = 0

    async def generate(self, messages, *, model, options):
        self.calls += 1
        return Generation("fixture", model, "stop", Usage(1, 1))


class Keys:
    async def is_active(self, key_hash):
        return True  # Only explicitly injected synthetic gw_ keys in these tests.


def make_app(*, policy=None, provider=None, admission=None, **kwargs):
    policy = policy or SafetyPolicy()
    provider = provider or Provider()
    store, telemetry = Store(), Telemetry()
    admission = admission or MemorySafetyStore(policy)
    app = create_app(
        gateway_api_key=KEY, providers={"openai": provider},
        model_aliases={"fast": ModelAlias("openai", {"openai": "fixture-model"})},
        redis_store=store, telemetry_repository=telemetry, price_book=PriceBook("test", {}),
        safety_policy=policy, safety_store=admission, **kwargs,
    )
    return app, store, telemetry, provider, admission


@pytest.mark.parametrize("changes", [
    {"max_tokens": 1_000_000_000}, {"max_tokens": 1025},
    {"messages": [{"role": "user", "content": "x"}] * 65},
    {"messages": [{"role": "user", "content": "x" * 16001}]},
    {"messages": [{"role": "user", "content": "x" * 10000}] * 4},
])
def test_request_bounds_stop_provider(changes):
    app, _, _, provider, _ = make_app()
    with TestClient(app) as client:
        response = client.post("/v1/chat", json={**BODY, **changes}, headers=HEADERS)
    assert response.status_code == 422
    assert provider.calls == 0


def test_configurable_bounds_allow_boundary_and_reject_excess():
    policy = SafetyPolicy(message_count=2, message_chars=8, total_chars=12, output_tokens=8)
    app, _, _, provider, _ = make_app(policy=policy)
    with TestClient(app) as client:
        good = {**BODY, "messages": [{"role": "user", "content": "123456"}] * 2, "max_tokens": 8}
        assert client.post("/v1/chat", json=good, headers=HEADERS).status_code == 200
        for bad in ({**good, "max_tokens": 9}, {**good, "messages": [{"role": "user", "content": "1234567"}] * 2}):
            assert client.post("/v1/chat", json=bad, headers=HEADERS).status_code == 422
    assert provider.calls == 1


def test_body_limit_precedes_auth_and_database_for_declared_and_streamed_bodies():
    app, store, telemetry, provider, _ = make_app(policy=SafetyPolicy(body_bytes=1024))
    with TestClient(app) as client:
        for content in (b"x" * 1025, iter([b"x" * 512, b"x" * 513])):
            response = client.post("/v1/chat", content=content)
            assert response.status_code == 413
            assert "X-Telemetry-Status" not in response.headers
    assert not telemetry.events and store.calls == provider.calls == 0


def test_early_throttle_rejects_invalid_requests_without_more_telemetry():
    policy = SafetyPolicy(ingress_limit=2, peer_limit=2)
    app, store, telemetry, provider, _ = make_app(policy=policy)
    with TestClient(app) as client:
        for forwarded in ("1.1.1.1", "2.2.2.2"):
            assert client.post("/v1/chat", json=BODY, headers={"X-Forwarded-For": forwarded}).status_code == 401
        rejected = client.post("/v1/chat", json=BODY, headers={"X-Forwarded-For": "3.3.3.3"})
        assert rejected.status_code == 429 and rejected.headers["Retry-After"] == "60"
        assert "X-Request-ID" not in rejected.headers
    assert len(telemetry.events) == 2
    assert store.calls == provider.calls == 0


def test_ingress_outage_fails_closed_before_telemetry():
    class Broken(MemorySafetyStore):
        async def allow_ingress(self, peer):
            raise ConnectionError("fixture failure")

    app, _, telemetry, provider, _ = make_app(admission=Broken(SafetyPolicy()))
    with TestClient(app) as client:
        assert client.get("/analytics/summary", headers=HEADERS).status_code == 503
        assert client.post("/v1/chat", json=BODY, headers=HEADERS).status_code == 503
    assert not telemetry.events and provider.calls == 0


def test_body_receive_timeout_and_invalid_length_are_early_rejections():
    async def run():
        app, _, telemetry, provider, _ = make_app(policy=SafetyPolicy(body_timeout=.01))
        messages = []
        async def receive():
            await asyncio.sleep(.1)
            return {"type": "http.request", "body": b"{}", "more_body": False}
        async def send(message):
            messages.append(message)
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http", "path": "/v1/chat", "raw_path": b"/v1/chat", "query_string": b"", "headers": [], "client": ("fixture", 1), "server": ("fixture", 80), "root_path": ""}
        await app(scope, receive, send)
        assert messages[0]["status"] == 408
        messages.clear()
        await app({**scope, "headers": [(b"content-length", b"-1")]}, receive, send)
        assert messages[0]["status"] == 400
        assert not telemetry.events and provider.calls == 0
    asyncio.run(run())


def test_global_and_per_key_concurrency_release_after_completion_and_cancellation():
    async def run():
        class Blocking(Provider):
            def __init__(self):
                super().__init__()
                self.release = asyncio.Event()
                self.started = asyncio.Queue()
            async def generate(self, messages, *, model, options):
                self.calls += 1
                await self.started.put(True)
                await self.release.wait()
                return Generation("fixture", model, "stop", Usage(1, 1))
        provider = Blocking()
        policy = SafetyPolicy(concurrency=2, key_concurrency=1)
        app, _, _, _, admission = make_app(policy=policy, provider=provider, key_repository=Keys())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
            async def send(key):
                return await client.post("/v1/chat", json=BODY, headers={"Authorization": "Bearer " + key})
            first = asyncio.create_task(send(KEY))
            await asyncio.wait_for(provider.started.get(), 1)
            assert (await send(KEY)).status_code == 429
            second = asyncio.create_task(send("gw_fixture_second"))
            await asyncio.wait_for(provider.started.get(), 1)
            assert (await send("gw_fixture_third")).status_code == 429
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert len(admission.leases) == 1
            provider.release.set()
            assert (await second).status_code == 200
            assert (await send(KEY)).status_code == 200
            assert not admission.leases
    asyncio.run(run())


@pytest.mark.parametrize("hanging", [False, True])
def test_concurrency_lease_released_on_failure_and_deadline(hanging):
    class Failure(Provider):
        async def generate(self, messages, *, model, options):
            if hanging:
                await asyncio.sleep(10)
            raise ProviderError("authentication")
    app, _, _, _, admission = make_app(provider=Failure(), retry_policy=RetryPolicy(deadline_seconds=.01))
    with TestClient(app) as client:
        response = client.post("/v1/chat", json=BODY, headers=HEADERS)
        assert response.status_code == (504 if hanging else 502)
    assert not admission.leases


@pytest.mark.parametrize("changes", [{"concurrency": 0}, {"output_tokens": 4097}, {"body_bytes": 1023}, {"body_timeout": float("nan")}, {"concurrency": 1, "key_concurrency": 2}])
def test_safety_configuration_is_bounded(changes):
    with pytest.raises(ValueError):
        SafetyPolicy(**changes)


def test_omitted_output_limit_uses_server_cap_but_explicit_excess_is_rejected():
    class CapturingProvider(Provider):
        seen = []
        async def generate(self, messages, *, model, options):
            self.seen.append(options.max_tokens)
            return await super().generate(messages, model=model, options=options)
    provider = CapturingProvider()
    app, _, _, _, _ = make_app(policy=SafetyPolicy(output_tokens=24), provider=provider)
    with TestClient(app) as client:
        assert client.post("/v1/chat", json=BODY, headers=HEADERS).status_code == 200
        assert client.post("/v1/chat", json={**BODY, "max_tokens": 25}, headers=HEADERS).status_code == 422
    assert provider.seen == [24]
