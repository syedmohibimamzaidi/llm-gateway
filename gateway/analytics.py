from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Protocol

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool


WINDOWS = {
    "1h": timedelta(hours=1),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}

METRICS = """
    COUNT(*)::integer AS request_count,
    COUNT(*) FILTER (WHERE status_code BETWEEN 200 AND 299)::integer AS success_count,
    COUNT(*) FILTER (WHERE cache_hit)::integer AS cache_hit_count,
    COUNT(*) FILTER (WHERE status_code >= 400)::integer AS error_count,
    COALESCE(SUM(input_tokens), 0)::bigint AS input_tokens,
    COALESCE(SUM(output_tokens), 0)::bigint AS output_tokens,
    COALESCE(SUM(estimated_cost_usd), 0)::numeric AS estimated_cost_usd,
    COUNT(*) FILTER (
        WHERE status_code BETWEEN 200 AND 299 AND NOT cache_hit
          AND estimated_cost_usd IS NULL
    )::integer AS unpriced_success_count,
    COALESCE(AVG(latency_ms), 0)::double precision AS mean_latency_ms,
    COALESCE(
        percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms), 0
    )::double precision AS p95_latency_ms
"""

FILTER = "gateway_key_id = %s AND created_at >= %s AND created_at < %s"
SUMMARY_SQL = f"SELECT {METRICS} FROM request_telemetry WHERE {FILTER}"
PROVIDERS_SQL = (
    f"SELECT COALESCE(final_provider, 'unrouted') AS provider, {METRICS} "
    f"FROM request_telemetry WHERE {FILTER} "
    "GROUP BY COALESCE(final_provider, 'unrouted') ORDER BY request_count DESC, provider"
)
COUNT_SQL = f"SELECT COUNT(*)::integer FROM request_telemetry WHERE {FILTER}"
REQUESTS_SQL = f"""
    SELECT request_id, created_at, model_alias, initial_provider, final_provider,
           actual_model, input_tokens, output_tokens, latency_ms, status_code,
           outcome, retry_count, failure_reason, cache_hit, estimated_cost_usd
    FROM request_telemetry WHERE {FILTER}
    ORDER BY created_at DESC, request_id DESC LIMIT %s OFFSET %s
"""


def window_bounds(window: str, now: datetime | None = None) -> tuple[datetime, datetime]:
    end = now or datetime.now(timezone.utc)
    return end - WINDOWS[window], end


def present_metrics(row: dict) -> dict:
    count = row["request_count"]
    result = dict(row)
    result["estimated_cost_usd"] = format(
        row["estimated_cost_usd"] or Decimal("0"), "f"
    )
    for name in ("success", "cache_hit", "error"):
        result[f"{name}_rate"] = row[f"{name}_count"] / count if count else 0.0
    result["mean_latency_ms"] = round(row["mean_latency_ms"], 3)
    result["p95_latency_ms"] = round(row["p95_latency_ms"], 3)
    return result


class AnalyticsRepository(Protocol):
    async def summary(self, key_id: str, start: datetime, end: datetime) -> dict: ...

    async def providers(self, key_id: str, start: datetime, end: datetime) -> list[dict]: ...

    async def requests(
        self, key_id: str, start: datetime, end: datetime, limit: int, offset: int
    ) -> tuple[int, list[dict]]: ...


class PostgresAnalyticsRepository:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool

    async def summary(self, key_id: str, start: datetime, end: datetime) -> dict:
        async with self.pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(SUMMARY_SQL, (key_id, start, end))
                return present_metrics(await cursor.fetchone())

    async def providers(self, key_id: str, start: datetime, end: datetime) -> list[dict]:
        async with self.pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(PROVIDERS_SQL, (key_id, start, end))
                return [
                    present_metrics(row)
                    for row in await cursor.fetchall()
                ]

    async def requests(
        self, key_id: str, start: datetime, end: datetime, limit: int, offset: int
    ) -> tuple[int, list[dict]]:
        parameters = (key_id, start, end)
        async with self.pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(COUNT_SQL, parameters)
                total = (await cursor.fetchone())["count"]
                await cursor.execute(REQUESTS_SQL, (*parameters, limit, offset))
                rows = await cursor.fetchall()
        return total, [
            {
                **row,
                "estimated_cost_usd": (
                    format(row["estimated_cost_usd"], "f")
                    if row["estimated_cost_usd"] is not None else None
                ),
            }
            for row in rows
        ]
