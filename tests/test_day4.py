from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError

from gateway.app import create_app
from gateway.config import ModelAlias
from gateway.providers.base import Generation, GenerationOptions, Message, ProviderError, Usage
from gateway.redis_store import RedisPolicy, RedisStore, make_cache_key
from gateway.routing import RetryPolicy


ALIASES = {
    "fast": ModelAlias(
        default_provider="openai",
        models={"openai": "openai-fast", "anthropic": "anthropic-fast"},
    ),
    "smart": ModelAlias(
        default_provider="anthropic",
        models={"anthropic": "anthropic-smart"},
    ),
}
HEADERS = {"Authorization": "Bearer secret"}
BODY = {"model": "fast", "messages": [{"role": "user", "content": "Hi"}], "temperature": 0, "cache": True}


class CountingProvider:
    name = "openai"

    def __init__(self) -> None:
        self.calls = 0
        self.failures: list[str] = []

    async def generate(
        self, messages: list[Message], *, model: str, options: GenerationOptions
    ) -> Generation:
        self.calls += 1
        if self.failures:
            raise ProviderError(self.failures.pop(0))
        return Generation("Hello!", model, "stop", Usage(3, 2))


class MemoryStore:
    def __init__(self, limit: int = 60) -> None:
        self.limit = limit
        self.counts: dict[str, int] = {}
        self.cached: dict[str, str] = {}
        self.allow_calls = 0
        self.get_calls = 0
        self.put_calls = 0
        self.fail_allow = False
        self.fail_get = False
        self.fail_put = False

    async def allow(self, key_hash: str) -> bool:
        self.allow_calls += 1
        if self.fail_allow:
            raise RedisConnectionError("Redis unavailable")
        self.counts[key_hash] = self.counts.get(key_hash, 0) + 1
        return self.counts[key_hash] <= self.limit

    async def get_cached(self, cache_key: str) -> str | None:
        self.get_calls += 1
        if self.fail_get:
            raise RedisConnectionError("Redis unavailable")
        return self.cached.get(cache_key)

    async def put_cached(self, cache_key: str, response_json: str) -> None:
        self.put_calls += 1
        if self.fail_put:
            raise RedisConnectionError("Redis unavailable")
        self.cached[cache_key] = response_json


def make_app(provider: CountingProvider, store: MemoryStore):
    return create_app(
        gateway_api_key="secret",
        providers={"openai": provider},
        model_aliases=ALIASES,
        retry_policy=RetryPolicy(max_retries=1, base_delay_seconds=0),
        redis_store=store,
    )


def test_cache_hit_returns_new_request_id_without_provider_call() -> None:
    provider = CountingProvider()
    store = MemoryStore()
    with TestClient(make_app(provider, store)) as client:
        first = client.post("/v1/chat", json=BODY, headers=HEADERS)
        second = client.post("/v1/chat", json=BODY, headers=HEADERS)
    assert first.status_code == second.status_code == 200
    assert not first.json()["cache_hit"]
    assert second.json()["cache_hit"]
    assert second.json()["request_id"] != first.json()["request_id"]
    assert second.json()["text"] == first.json()["text"]
    assert second.json()["usage"] == first.json()["usage"]
    assert second.json()["attempts"] == []
    assert provider.calls == 1
    assert store.allow_calls == 2
    assert store.get_calls == 2
    assert store.put_calls == 1


def test_rate_limit_runs_before_cache_lookup_and_counts_hits() -> None:
    provider = CountingProvider()
    store = MemoryStore(limit=1)
    with TestClient(make_app(provider, store)) as client:
        assert client.post("/v1/chat", json=BODY, headers=HEADERS).status_code == 200
        limited = client.post("/v1/chat", json=BODY, headers=HEADERS)
    assert limited.status_code == 429
    assert limited.json() == {"detail": "Gateway rate limit exceeded"}
    assert store.allow_calls == 2
    assert store.get_calls == 1
    assert provider.calls == 1


def test_cache_is_opt_in_and_requires_repeatable_request() -> None:
    provider = CountingProvider()
    store = MemoryStore()
    requests = [
        {**BODY, "cache": False},
        {**BODY, "temperature": None},
        {**BODY, "temperature": 0.5},
        {**BODY, "messages": [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Partial answer"},
        ]},
    ]
    with TestClient(make_app(provider, store)) as client:
        for body in requests:
            response = client.post("/v1/chat", json=body, headers=HEADERS)
            assert response.status_code == 200
            assert not response.json()["cache_hit"]
    assert provider.calls == len(requests)
    assert store.get_calls == 0
    assert store.put_calls == 0


def test_retried_result_is_not_cached() -> None:
    provider = CountingProvider()
    provider.failures = ["timeout"]
    store = MemoryStore()
    with TestClient(make_app(provider, store)) as client:
        first = client.post("/v1/chat", json=BODY, headers=HEADERS)
        second = client.post("/v1/chat", json=BODY, headers=HEADERS)
    assert first.status_code == second.status_code == 200
    assert first.json()["retry_count"] == 1
    assert provider.calls == 3
    assert store.put_calls == 1
    assert not second.json()["cache_hit"]


def test_rate_limiter_unavailable_fails_closed_before_provider() -> None:
    provider = CountingProvider()
    store = MemoryStore()
    store.fail_allow = True
    with TestClient(make_app(provider, store)) as client:
        response = client.post("/v1/chat", json=BODY, headers=HEADERS)
    assert response.status_code == 503
    assert response.json() == {"detail": "Rate limiter unavailable"}
    assert store.get_calls == 0
    assert provider.calls == 0


def test_cache_read_and_write_errors_do_not_fail_allowed_request() -> None:
    provider = CountingProvider()
    store = MemoryStore()
    store.fail_get = True
    store.fail_put = True
    with TestClient(make_app(provider, store)) as client:
        response = client.post("/v1/chat", json=BODY, headers=HEADERS)
    assert response.status_code == 200
    assert not response.json()["cache_hit"]
    assert provider.calls == 1
    assert store.get_calls == store.put_calls == 1


def test_cache_key_separates_provider_model_messages_options_and_key() -> None:
    base = {
        "key_hash": "caller-one",
        "provider": "openai",
        "model": "model-a",
        "messages": [Message("user", "private prompt")],
        "options": GenerationOptions(max_tokens=100, temperature=0),
    }
    original = make_cache_key(**base)
    variants = [
        {**base, "key_hash": "caller-two"},
        {**base, "provider": "anthropic"},
        {**base, "model": "model-b"},
        {**base, "messages": [Message("user", "different prompt")]},
        {**base, "options": GenerationOptions(max_tokens=101, temperature=0)},
        {**base, "options": GenerationOptions(max_tokens=100, temperature=0.5)},
    ]
    assert all(make_cache_key(**variant) != original for variant in variants)
    assert "private prompt" not in original


def test_redis_store_uses_atomic_counter_and_cache_ttl() -> None:
    class FakeRedisClient:
        def __init__(self) -> None:
            self.counts: dict[str, int] = {}
            self.values: dict[str, str] = {}
            self.window: int | None = None
            self.ttl: int | None = None

        async def eval(self, script: str, numkeys: int, key: str, window: int) -> int:
            assert "INCR" in script and "EXPIRE" in script
            assert numkeys == 1
            self.window = window
            self.counts[key] = self.counts.get(key, 0) + 1
            return self.counts[key]

        async def get(self, key: str) -> str | None:
            return self.values.get(key)

        async def set(self, key: str, value: str, *, ex: int) -> None:
            self.ttl = ex
            self.values[key] = value

    import asyncio

    async def run() -> None:
        client = FakeRedisClient()
        store = RedisStore(client, RedisPolicy(2, 30, 45))
        assert await store.allow("hash-one")
        assert await store.allow("hash-one")
        assert not await store.allow("hash-one")
        assert await store.allow("hash-two")
        assert client.window == 30
        await store.put_cached("cache-key", "response")
        assert client.ttl == 45
        assert await store.get_cached("cache-key") == "response"

    asyncio.run(run())
