import os
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from gateway.routing import Attempt


@dataclass
class RequestTelemetry:
    request_id: UUID
    price_version: str
    gateway_key_id: str | None = None
    model_alias: str | None = None
    initial_provider: str | None = None
    final_provider: str | None = None
    actual_model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: float = 0.0
    status_code: int = 500
    outcome: str = "error"
    retry_count: int = 0
    failure_reason: str | None = None
    cache_hit: bool = False
    estimated_cost_usd: Decimal | None = None
    attempts: tuple[Attempt, ...] = field(default_factory=tuple)

    def parameters(self) -> tuple[object, ...]:
        return (
            self.request_id,
            self.gateway_key_id,
            self.model_alias,
            self.initial_provider,
            self.final_provider,
            self.actual_model,
            self.input_tokens,
            self.output_tokens,
            self.latency_ms,
            self.status_code,
            self.outcome,
            self.retry_count,
            self.failure_reason,
            self.cache_hit,
            self.estimated_cost_usd,
            self.price_version,
            Jsonb([
                {
                    "provider": attempt.provider,
                    "model": attempt.model,
                    "failure_reason": attempt.failure_reason,
                }
                for attempt in self.attempts
            ]),
        )


class TelemetryRepository(Protocol):
    async def record(self, event: RequestTelemetry) -> None: ...


INSERT_COLUMNS = (
    "request_id", "gateway_key_id", "model_alias", "initial_provider",
    "final_provider", "actual_model", "input_tokens", "output_tokens",
    "latency_ms", "status_code", "outcome", "retry_count", "failure_reason",
    "cache_hit", "estimated_cost_usd", "price_version", "attempts",
)
INSERT_SQL = (
    "INSERT INTO request_telemetry (" + ", ".join(INSERT_COLUMNS) + ") VALUES ("
    + ", ".join(["%s"] * len(INSERT_COLUMNS)) + ")"
)


class PostgresTelemetryRepository:
    def __init__(self, dsn: str) -> None:
        self.pool = AsyncConnectionPool(
            conninfo=dsn,
            min_size=0,
            max_size=5,
            timeout=0.5,
            kwargs={"connect_timeout": 2},
            open=False,
        )

    @classmethod
    def from_env(cls) -> "PostgresTelemetryRepository | None":
        dsn = os.getenv("DATABASE_URL")
        return cls(dsn) if dsn else None

    async def open(self) -> None:
        await self.pool.open(wait=False)

    async def close(self) -> None:
        await self.pool.close()

    async def ready(self) -> bool:
        async with self.pool.connection() as connection:
            versions = await (await connection.execute(
                "SELECT version FROM schema_migrations WHERE version = ANY(%s)",
                (["001_request_telemetry", "002_gateway_keys"],),
            )).fetchall()
            await connection.execute("SELECT " + ", ".join(INSERT_COLUMNS) + ", created_at FROM request_telemetry LIMIT 0")
            await connection.execute("SELECT id, key_hash, name, created_at, revoked_at FROM gateway_keys LIMIT 0")
        return len(versions) == 2

    async def record(self, event: RequestTelemetry) -> None:
        async with self.pool.connection() as connection:
            await connection.execute(INSERT_SQL, event.parameters())
