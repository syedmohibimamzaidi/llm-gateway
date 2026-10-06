"""Hashed managed gateway keys. Raw keys never enter this repository."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from psycopg_pool import AsyncConnectionPool


@dataclass(frozen=True)
class KeyRecord:
    id: UUID
    name: str
    created_at: datetime
    revoked_at: datetime | None


class KeyRepository(Protocol):
    async def is_active(self, key_hash: str) -> bool: ...
    async def create(self, key_id: UUID, key_hash: str, name: str) -> KeyRecord: ...
    async def list(self) -> list[KeyRecord]: ...
    async def revoke(self, key_id: UUID) -> KeyRecord | None: ...


def _record(row: tuple) -> KeyRecord:
    return KeyRecord(*row)


class PostgresKeyRepository:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool

    async def is_active(self, key_hash: str) -> bool:
        async with self.pool.connection() as connection:
            row = await (await connection.execute(
                "SELECT 1 FROM gateway_keys WHERE key_hash = %s AND revoked_at IS NULL",
                (key_hash,),
            )).fetchone()
        return row is not None

    async def create(self, key_id: UUID, key_hash: str, name: str) -> KeyRecord:
        async with self.pool.connection() as connection:
            row = await (await connection.execute(
                "INSERT INTO gateway_keys (id, key_hash, name) VALUES (%s, %s, %s) "
                "RETURNING id, name, created_at, revoked_at",
                (key_id, key_hash, name),
            )).fetchone()
        return _record(row)

    async def list(self) -> list[KeyRecord]:
        async with self.pool.connection() as connection:
            rows = await (await connection.execute(
                "SELECT id, name, created_at, revoked_at FROM gateway_keys "
                "ORDER BY created_at DESC, id DESC"
            )).fetchall()
        return [_record(row) for row in rows]

    async def revoke(self, key_id: UUID) -> KeyRecord | None:
        async with self.pool.connection() as connection:
            row = await (await connection.execute(
                "UPDATE gateway_keys SET revoked_at = COALESCE(revoked_at, NOW()) "
                "WHERE id = %s RETURNING id, name, created_at, revoked_at",
                (key_id,),
            )).fetchone()
        return _record(row) if row is not None else None
