"""Bounded ingress and distributed provider admission, independent of telemetry."""

import asyncio
import hashlib
import math
import os
import time
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from redis.exceptions import RedisError
from starlette.responses import JSONResponse


@dataclass(frozen=True)
class SafetyPolicy:
    body_bytes: int = 65536
    message_count: int = 64
    message_chars: int = 16000
    total_chars: int = 32000
    output_tokens: int = 1024
    concurrency: int = 8
    key_concurrency: int = 2
    ingress_limit: int = 300
    peer_limit: int = 120
    ingress_window: int = 60
    body_timeout: float = 5

    def __post_init__(self):
        bounds = {
            "body_bytes": (1024, 1048576), "message_count": (1, 64),
            "message_chars": (1, 16000), "total_chars": (1, 64000),
            "output_tokens": (1, 4096), "concurrency": (1, 32),
            "key_concurrency": (1, 8), "ingress_limit": (1, 10000),
            "peer_limit": (1, 10000), "ingress_window": (1, 3600),
        }
        if any(not low <= getattr(self, name) <= high for name, (low, high) in bounds.items()):
            raise ValueError("Invalid gateway safety limits")
        if not math.isfinite(self.body_timeout) or not 0 < self.body_timeout <= 10:
            raise ValueError("Invalid gateway body timeout")
        if self.key_concurrency > self.concurrency:
            raise ValueError("Per-key concurrency exceeds global concurrency")

    @classmethod
    def from_env(cls):
        names = {
            "body_bytes": "MAX_BODY_BYTES", "message_count": "MAX_MESSAGES",
            "message_chars": "MAX_MESSAGE_CHARS", "total_chars": "MAX_TOTAL_CHARS",
            "output_tokens": "MAX_OUTPUT_TOKENS", "concurrency": "MAX_CONCURRENCY",
            "key_concurrency": "MAX_KEY_CONCURRENCY", "ingress_limit": "INGRESS_LIMIT",
            "peer_limit": "INGRESS_PEER_LIMIT", "ingress_window": "INGRESS_WINDOW_SECONDS",
        }
        defaults = cls()
        return cls(**{name: int(os.getenv("GATEWAY_" + env, str(getattr(defaults, name))))
                      for name, env in names.items()})

    def accepts(self, request) -> bool:
        return (
            len(request.messages) <= self.message_count
            and all(len(message.content) <= self.message_chars for message in request.messages)
            and sum(len(message.content) for message in request.messages) <= self.total_chars
            and request.max_tokens <= self.output_tokens
        )


class SafetyStore(Protocol):
    async def allow_ingress(self, peer: str) -> bool: ...
    async def acquire(self, key: str, ttl: float) -> str | None: ...
    async def release(self, key: str, lease: str) -> None: ...


class MemorySafetyStore:
    """Development/test fallback; production always uses Redis."""

    def __init__(self, policy: SafetyPolicy):
        self.policy = policy
        self.window_end = 0.0
        self.count = 0
        self.peers: dict[str, int] = {}
        self.leases: dict[str, tuple[str, float]] = {}

    async def allow_ingress(self, peer):
        now = time.monotonic()
        if now >= self.window_end:
            self.window_end = now + self.policy.ingress_window
            self.count, self.peers = 0, {}
        self.count += 1
        if self.count > self.policy.ingress_limit:
            return False
        self.peers[peer] = self.peers.get(peer, 0) + 1
        return self.peers[peer] <= self.policy.peer_limit

    async def acquire(self, key, ttl):
        now = time.monotonic()
        self.leases = {token: value for token, value in self.leases.items() if value[1] > now}
        if len(self.leases) >= self.policy.concurrency or sum(k == key for k, _ in self.leases.values()) >= self.policy.key_concurrency:
            return None
        lease = uuid4().hex
        self.leases[lease] = (key, now + ttl)
        return lease

    async def release(self, key, lease):
        self.leases.pop(lease, None)


INGRESS_SCRIPT = """
local n = redis.call('INCR', KEYS[1])
if n == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
if n > tonumber(ARGV[2]) then return 0 end
local p = redis.call('INCR', KEYS[2])
if p == 1 then redis.call('EXPIRE', KEYS[2], ARGV[1]) end
return p <= tonumber(ARGV[3]) and 1 or 0
"""
ACQUIRE_SCRIPT = """
local clock = redis.call('TIME')
local now = clock[1] * 1000 + math.floor(clock[2] / 1000)
for _, key in ipairs(KEYS) do redis.call('ZREMRANGEBYSCORE', key, '-inf', now) end
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[1]) or
   redis.call('ZCARD', KEYS[2]) >= tonumber(ARGV[2]) then return 0 end
for _, key in ipairs(KEYS) do
  redis.call('ZADD', key, now + tonumber(ARGV[3]), ARGV[4])
  local last = redis.call('ZREVRANGE', key, 0, 0, 'WITHSCORES')
  redis.call('PEXPIREAT', key, math.ceil(tonumber(last[2])) + 1000)
end
return 1
"""
RELEASE_SCRIPT = """
for _, key in ipairs(KEYS) do redis.call('ZREM', key, ARGV[1]) end
return 1
"""


class RedisSafetyStore:
    def __init__(self, client, policy: SafetyPolicy, namespace="gateway:safety:v1"):
        self.client, self.policy, self.namespace = client, policy, namespace

    async def allow_ingress(self, peer):
        peer_hash = hashlib.sha256(peer.encode()).hexdigest()
        return bool(await self.client.eval(
            INGRESS_SCRIPT, 2, self.namespace + ":ingress", self.namespace + ":peer:" + peer_hash,
            self.policy.ingress_window, self.policy.ingress_limit, self.policy.peer_limit,
        ))

    async def acquire(self, key, ttl):
        lease = uuid4().hex
        allowed = await self.client.eval(
            ACQUIRE_SCRIPT, 2, self.namespace + ":active", self.namespace + ":active:" + key,
            self.policy.concurrency, self.policy.key_concurrency, math.ceil(ttl * 1000), lease,
        )
        return lease if allowed else None

    async def release(self, key, lease):
        await self.client.eval(RELEASE_SCRIPT, 2, self.namespace + ":active", self.namespace + ":active:" + key, lease)


class IngressGuard:
    """Outer ASGI guard: throttle and bound bytes before auth, parsing or telemetry.

    Uses the transport peer only. Untrusted X-Forwarded-For cannot create new
    buckets. Behind Render, proxy peers may share a bucket; the global limit
    deliberately remains authoritative for this small private demo.
    """

    def __init__(self, app, *, store: SafetyStore, policy: SafetyPolicy, production=False):
        self.app, self.store, self.policy, self.production = app, store, policy, production

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def safe_send(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers += [(b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"no-referrer")]
                if self.production:
                    headers += [(b"strict-transport-security", b"max-age=31536000"),
                                (b"content-security-policy", b"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")]
                message = {**message, "headers": headers}
            await send(message)

        async def reject(status, detail, retry=None):
            headers = {"Cache-Control": "no-store"}
            if retry is not None:
                headers["Retry-After"] = str(retry)
            await JSONResponse({"detail": detail}, status_code=status, headers=headers)(scope, receive, safe_send)

        path = scope["path"]
        protected = path.startswith(("/v1/", "/analytics", "/api-keys")) or path == "/requests"
        if not protected:
            return await self.app(scope, receive, safe_send)
        peer = (scope.get("client") or ("unknown", 0))[0]
        try:
            async with asyncio.timeout(0.75):
                allowed = await self.store.allow_ingress(peer)
        except (RedisError, TimeoutError):
            return await reject(503, "Admission control unavailable")
        if not allowed:
            return await reject(429, "Request limit exceeded", self.policy.ingress_window)
        headers = scope.get("headers", [])
        if sum(len(k) + len(v) for k, v in headers) > 16384:
            return await reject(431, "Request headers too large")
        lengths = [v for k, v in headers if k.lower() == b"content-length"]
        if lengths:
            try:
                length = int(lengths[0])
                if length < 0 or len(lengths) != 1:
                    raise ValueError
            except ValueError:
                return await reject(400, "Invalid content length")
            if length > self.policy.body_bytes:
                return await reject(413, "Request body too large")
        body = bytearray()
        try:
            async with asyncio.timeout(self.policy.body_timeout):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    chunk = message.get("body", b"")
                    if len(body) + len(chunk) > self.policy.body_bytes:
                        return await reject(413, "Request body too large")
                    body.extend(chunk)
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            return await reject(408, "Request body timeout")
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, safe_send)
