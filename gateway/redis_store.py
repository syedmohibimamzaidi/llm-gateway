import hashlib
import json
import os
from dataclasses import dataclass
from typing import Protocol, Sequence

import redis.asyncio as redis

from gateway.providers.base import GenerationOptions, Message


RATE_LIMIT_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return count
"""


@dataclass(frozen=True)
class RedisPolicy:
    requests_per_window: int = 60
    window_seconds: int = 60
    cache_ttl_seconds: int = 60

    def __post_init__(self) -> None:
        if not 1 <= self.requests_per_window <= 10000:
            raise ValueError("requests_per_window must be between 1 and 10000")
        if not 1 <= self.window_seconds <= 3600:
            raise ValueError("window_seconds must be between 1 and 3600")
        if not 1 <= self.cache_ttl_seconds <= 3600:
            raise ValueError("cache_ttl_seconds must be between 1 and 3600")

    @classmethod
    def from_env(cls) -> "RedisPolicy":
        return cls(
            requests_per_window=int(os.getenv("GATEWAY_RATE_LIMIT", "60")),
            window_seconds=int(os.getenv("GATEWAY_RATE_WINDOW_SECONDS", "60")),
            cache_ttl_seconds=int(os.getenv("GATEWAY_CACHE_TTL_SECONDS", "60")),
        )


class GatewayStore(Protocol):
    async def allow(self, key_hash: str) -> bool: ...

    async def get_cached(self, cache_key: str) -> str | None: ...

    async def put_cached(self, cache_key: str, response_json: str) -> None: ...


class RedisStore:
    def __init__(self, client: redis.Redis, policy: RedisPolicy) -> None:
        self.client = client
        self.policy = policy

    @classmethod
    def from_env(cls) -> "RedisStore":
        client = redis.from_url(
            os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            decode_responses=True,
            socket_connect_timeout=0.25,
            socket_timeout=0.25,
        )
        return cls(client, RedisPolicy.from_env())

    async def allow(self, key_hash: str) -> bool:
        count = await self.client.eval(
            RATE_LIMIT_SCRIPT,
            1,
            f"gateway:rate:v1:{key_hash}",
            self.policy.window_seconds,
        )
        return int(count) <= self.policy.requests_per_window

    async def get_cached(self, cache_key: str) -> str | None:
        return await self.client.get(cache_key)

    async def put_cached(self, cache_key: str, response_json: str) -> None:
        await self.client.set(cache_key, response_json, ex=self.policy.cache_ttl_seconds)

    async def aclose(self) -> None:
        await self.client.aclose()


def make_cache_key(
    *,
    key_hash: str,
    provider: str,
    model: str,
    messages: Sequence[Message],
    options: GenerationOptions,
) -> str:
    fingerprint = json.dumps(
        {
            "version": 1,
            "key_hash": key_hash,
            "provider": provider,
            "model": model,
            "messages": [{"role": item.role, "content": item.content} for item in messages],
            "max_tokens": options.max_tokens,
            "temperature": options.temperature,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "gateway:cache:v1:" + hashlib.sha256(fingerprint.encode()).hexdigest()
