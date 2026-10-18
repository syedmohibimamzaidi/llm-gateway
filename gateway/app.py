import asyncio
import hashlib
import hmac
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Awaitable, Literal, Mapping, TypeVar
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from redis.exceptions import RedisError

from gateway.analytics import AnalyticsRepository, PostgresAnalyticsRepository, window_bounds
from gateway.config import ModelAlias, load_model_aliases
from gateway.deployment import Deployment
from gateway.safety import IngressGuard, MemorySafetyStore, RedisSafetyStore, SafetyPolicy, SafetyStore
from gateway.keys import KeyRecord, KeyRepository, PostgresKeyRepository
from gateway.providers.anthropic import AnthropicProvider
from gateway.providers.base import FailureReason, GenerationOptions, Message, Provider
from gateway.providers.openai import OpenAIProvider
from gateway.pricing import PriceBook
from gateway.redis_store import GatewayStore, RedisStore, make_cache_key
from gateway.routing import RetryPolicy, RouteFailure, generate_with_fallback
from gateway.telemetry import PostgresTelemetryRepository, RequestTelemetry, TelemetryRepository


logger = logging.getLogger(__name__)
T = TypeVar("T")


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str = Field(pattern="^(system|user|assistant)$")
    content: str = Field(min_length=1, max_length=16000)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1, max_length=100)
    messages: list[ChatMessage] = Field(min_length=1, max_length=64)
    max_tokens: int = Field(default=1024, ge=1, le=4096)
    temperature: float | None = Field(default=None, ge=0, le=1)
    provider: Literal["openai", "anthropic"] | None = None
    cache: bool = False

    @model_validator(mode="after")
    def validate_conversation(self) -> "ChatRequest":
        if not any(message.role == "user" for message in self.messages):
            raise ValueError("At least one user message is required")
        seen_non_system = False
        for message in self.messages:
            if message.role == "system" and seen_non_system:
                raise ValueError("System messages must precede conversation messages")
            if message.role != "system":
                seen_non_system = True
        return self


class ChatUsage(BaseModel):
    input_tokens: int
    output_tokens: int


class AttemptResponse(BaseModel):
    provider: str
    model: str
    failure_reason: FailureReason | None


class ChatResponse(BaseModel):
    request_id: str
    provider: str
    model: str
    text: str
    finish_reason: str | None
    usage: ChatUsage
    initial_provider: str
    final_provider: str
    retry_count: int
    failure_reason: FailureReason | None
    attempts: list[AttemptResponse]
    cache_hit: bool = False


class KeyCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)


class KeyMetadata(BaseModel):
    id: UUID
    name: str
    created_at: datetime
    revoked_at: datetime | None


class KeyCreated(KeyMetadata):
    key: str


def create_app(
    *,
    gateway_api_key: str | None = None,
    providers: Mapping[str, Provider] | None = None,
    model_aliases: Mapping[str, ModelAlias] | None = None,
    retry_policy: RetryPolicy | None = None,
    redis_store: GatewayStore | None = None,
    price_book: PriceBook | None = None,
    telemetry_repository: TelemetryRepository | None = None,
    analytics_repository: AnalyticsRepository | None = None,
    key_repository: KeyRepository | None = None,
    safety_policy: SafetyPolicy | None = None,
    safety_store: SafetyStore | None = None,
) -> FastAPI:
    deployment = Deployment.from_env()
    safety = safety_policy if safety_policy is not None else SafetyPolicy.from_env()
    configured_key = gateway_api_key if gateway_api_key is not None else os.getenv("GATEWAY_API_KEY", "")
    configured_key_hash = hashlib.sha256(configured_key.encode()).digest() if configured_key else None
    aliases = model_aliases if model_aliases is not None else load_model_aliases()
    policy = retry_policy if retry_policy is not None else RetryPolicy.from_env()
    store = redis_store if redis_store is not None else RedisStore.from_env()
    prices = price_book if price_book is not None else PriceBook.from_env()
    repository = (
        telemetry_repository
        if telemetry_repository is not None
        else PostgresTelemetryRepository.from_env()
    )
    analytics = analytics_repository or (
        PostgresAnalyticsRepository(repository.pool)
        if isinstance(repository, PostgresTelemetryRepository) else None
    )
    keys = key_repository or (
        PostgresKeyRepository(repository.pool)
        if isinstance(repository, PostgresTelemetryRepository) else None
    )
    selected_providers = dict(providers) if providers is not None else {}
    if providers is None:
        if provider_key := os.getenv("OPENAI_API_KEY"):
            selected_providers["openai"] = OpenAIProvider(api_key=provider_key)
        if provider_key := os.getenv("ANTHROPIC_API_KEY"):
            selected_providers["anthropic"] = AnthropicProvider(api_key=provider_key)

    deployment.validate(
        key=configured_key, aliases=aliases, providers=selected_providers, prices=prices,
        database_url=os.getenv("DATABASE_URL"), redis_url=os.getenv("REDIS_URL"),
    )
    if deployment.production and not isinstance(store, RedisStore):
        if safety_store is None:
            raise ValueError("Production requires distributed admission control")
    admission = safety_store or (
        RedisSafetyStore(store.client, safety) if deployment.production and isinstance(store, RedisStore)
        else MemorySafetyStore(safety)
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if isinstance(repository, PostgresTelemetryRepository):
            await repository.open()
        try:
            yield
        finally:
            if isinstance(repository, PostgresTelemetryRepository):
                await repository.close()
            if isinstance(store, RedisStore) and redis_store is None:
                await store.aclose()
            for provider in selected_providers.values():
                if isinstance(provider, (OpenAIProvider, AnthropicProvider)):
                    await provider.aclose()

    app = FastAPI(
        title="LLM Gateway", lifespan=lifespan,
        docs_url=None if deployment.production else "/docs",
        redoc_url=None if deployment.production else "/redoc",
        openapi_url=None if deployment.production else "/openapi.json",
    )

    @app.middleware("http")
    async def record_chat_request(http_request: Request, call_next):
        if http_request.url.path != "/v1/chat" or http_request.method != "POST":
            return await call_next(http_request)
        trace = RequestTelemetry(request_id=uuid4(), price_version=prices.version)
        http_request.state.telemetry = trace
        started = time.perf_counter()
        response: Response | None = None
        try:
            try:
                trace.gateway_key_id = await authenticate_gateway_key(
                    http_request.headers.get("authorization")
                )
            except HTTPException as exc:
                response = JSONResponse(
                    status_code=exc.status_code,
                    content={"detail": exc.detail},
                    headers=exc.headers,
                )
            else:
                response = await call_next(http_request)
            trace.status_code = response.status_code
            response.headers["X-Request-ID"] = str(trace.request_id)
            return response
        except Exception:
            trace.status_code = 500
            raise
        finally:
            trace.latency_ms = round((time.perf_counter() - started) * 1000, 3)
            if trace.status_code == 200:
                trace.outcome = "cache_hit" if trace.cache_hit else "success"
            else:
                trace.outcome = {
                    401: "unauthorized",
                    422: "invalid_request",
                    429: "rate_limited",
                    503: "unavailable",
                    504: "timeout",
                }.get(trace.status_code, "error")
            if repository is None:
                if response is not None:
                    response.headers["X-Telemetry-Status"] = "disabled"
            else:
                try:
                    async with asyncio.timeout(0.75):
                        await repository.record(trace)
                except Exception as exc:
                    logger.warning(
                        "PostgreSQL telemetry write failed for request %s: %s",
                        trace.request_id, type(exc).__name__,
                    )
                    if response is not None:
                        response.headers["X-Telemetry-Status"] = "failed"
                else:
                    if response is not None:
                        response.headers["X-Telemetry-Status"] = "stored"

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    readiness_lock = asyncio.Lock()
    readiness_at, readiness_ok = 0.0, False

    @app.get("/readyz")
    async def ready() -> JSONResponse:
        nonlocal readiness_at, readiness_ok
        async with readiness_lock:
            if time.monotonic() - readiness_at >= 1:
                try:
                    async with asyncio.timeout(2):
                        readiness_ok = (
                            isinstance(store, RedisStore)
                            and isinstance(repository, PostgresTelemetryRepository)
                            and bool(await store.client.ping())
                            and await repository.ready()
                        )
                except Exception as exc:
                    readiness_ok = False
                    logger.warning("Readiness check failed: %s", type(exc).__name__)
                readiness_at = time.monotonic()
        return JSONResponse(
            {"status": "ready" if readiness_ok else "not_ready"},
            status_code=200 if readiness_ok else 503, headers={"Cache-Control": "no-store"},
        )

    async def authenticate_gateway_key(authorization: Annotated[str | None, Header()] = None) -> str:
        if configured_key_hash is None:
            raise HTTPException(status_code=503, detail="Gateway API key is not configured")
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token or token != token.strip() or " " in token or len(token) > 256:
            raise HTTPException(status_code=401, detail="Invalid gateway API key")
        supplied_hash = hashlib.sha256(token.encode()).digest()
        if hmac.compare_digest(supplied_hash, configured_key_hash):
            return configured_key_hash.hex()
        if not token.startswith("gw_") or keys is None:
            raise HTTPException(status_code=401, detail="Invalid gateway API key")
        if await read_keys(keys.is_active(supplied_hash.hex())):
            return supplied_hash.hex()
        raise HTTPException(status_code=401, detail="Invalid gateway API key")

    async def authenticate_manager(authorization: Annotated[str | None, Header()] = None) -> str:
        key_id = await authenticate_gateway_key(authorization)
        if key_id != configured_key_hash.hex():
            raise HTTPException(status_code=403, detail="Key management requires the configured gateway key")
        return key_id

    async def read_keys(operation: Awaitable[T]) -> T:
        try:
            async with asyncio.timeout(3):
                return await operation
        except Exception as exc:
            logger.warning("PostgreSQL key operation failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail="Key management unavailable") from exc

    def key_source() -> KeyRepository:
        if keys is None:
            raise HTTPException(status_code=503, detail="Key management is not configured")
        return keys

    def key_metadata(record: KeyRecord) -> KeyMetadata:
        return KeyMetadata(id=record.id, name=record.name,
                           created_at=record.created_at, revoked_at=record.revoked_at)

    @app.post("/api-keys", response_model=KeyCreated, status_code=201)
    async def create_key(
        request: KeyCreateRequest,
        response: Response,
        _: Annotated[str, Depends(authenticate_manager)],
    ) -> KeyCreated:
        response.headers["Cache-Control"] = "no-store"
        raw_key = "gw_" + secrets.token_urlsafe(32)
        record = await read_keys(key_source().create(
            uuid4(), hashlib.sha256(raw_key.encode()).hexdigest(), request.name
        ))
        return KeyCreated(**key_metadata(record).model_dump(), key=raw_key)

    @app.get("/api-keys", response_model=list[KeyMetadata])
    async def list_keys(
        response: Response,
        _: Annotated[str, Depends(authenticate_manager)],
    ) -> list[KeyMetadata]:
        response.headers["Cache-Control"] = "no-store"
        return [key_metadata(record) for record in await read_keys(key_source().list())]

    @app.delete("/api-keys/{key_id}", response_model=KeyMetadata)
    async def revoke_key(
        key_id: UUID,
        response: Response,
        _: Annotated[str, Depends(authenticate_manager)],
    ) -> KeyMetadata:
        response.headers["Cache-Control"] = "no-store"
        record = await read_keys(key_source().revoke(key_id))
        if record is None:
            raise HTTPException(status_code=404, detail="Gateway key not found")
        return key_metadata(record)

    async def read_analytics(operation: Awaitable[T]) -> T:
        try:
            async with asyncio.timeout(3):
                return await operation
        except Exception as exc:
            logger.warning("PostgreSQL analytics read failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail="Analytics unavailable") from exc

    def analytics_source() -> AnalyticsRepository:
        if analytics is None:
            raise HTTPException(status_code=503, detail="Analytics is not configured")
        return analytics

    @app.get("/analytics/summary")
    async def analytics_summary(
        response: Response,
        key_id: Annotated[str, Depends(authenticate_gateway_key)],
        window: Literal["1h", "24h", "7d", "30d"] = "24h",
    ) -> dict:
        response.headers["Cache-Control"] = "no-store"
        start, end = window_bounds(window)
        metrics = await read_analytics(analytics_source().summary(key_id, start, end))
        return {"window": window, "start": start, "end": end, **metrics}

    @app.get("/analytics/providers")
    async def analytics_providers(
        response: Response,
        key_id: Annotated[str, Depends(authenticate_gateway_key)],
        window: Literal["1h", "24h", "7d", "30d"] = "24h",
    ) -> dict:
        response.headers["Cache-Control"] = "no-store"
        start, end = window_bounds(window)
        providers = await read_analytics(analytics_source().providers(key_id, start, end))
        return {"window": window, "start": start, "end": end, "providers": providers}

    @app.get("/requests")
    async def analytics_requests(
        response: Response,
        key_id: Annotated[str, Depends(authenticate_gateway_key)],
        window: Literal["1h", "24h", "7d", "30d"] = "24h",
        limit: int = Query(default=25, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
    ) -> dict:
        response.headers["Cache-Control"] = "no-store"
        start, end = window_bounds(window)
        total, items = await read_analytics(
            analytics_source().requests(key_id, start, end, limit, offset)
        )
        return {
            "window": window, "start": start, "end": end,
            "total": total, "limit": limit, "offset": offset, "items": items,
        }

    @app.post("/v1/chat", response_model=ChatResponse)
    async def chat(
        request: ChatRequest,
        http_request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ) -> ChatResponse:
        trace: RequestTelemetry = http_request.state.telemetry
        trace.model_alias = request.model
        if "max_tokens" not in request.model_fields_set:
            request = request.model_copy(update={"max_tokens": min(request.max_tokens, safety.output_tokens)})
        if not safety.accepts(request):
            raise HTTPException(status_code=422, detail="Request exceeds gateway limits")
        if not deployment.paid_enabled:
            raise HTTPException(status_code=503, detail="Provider access is disabled")
        assert trace.gateway_key_id is not None
        try:
            allowed = await store.allow(trace.gateway_key_id)
        except RedisError as exc:
            logger.warning("Redis rate limiter is unavailable: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail="Rate limiter unavailable") from exc
        if not allowed:
            raise HTTPException(status_code=429, detail="Gateway rate limit exceeded")
        if not aliases:
            raise HTTPException(status_code=503, detail="Model aliases are not configured")
        route = aliases.get(request.model)
        if route is None:
            raise HTTPException(status_code=422, detail="Unknown model alias")
        provider_name = request.provider or route.default_provider
        model = route.models.get(provider_name)
        if model is None:
            raise HTTPException(status_code=422, detail="Provider is not configured for this alias")
        trace.initial_provider = provider_name
        trace.final_provider = provider_name
        trace.actual_model = model
        selected_provider = selected_providers.get(provider_name)
        if selected_provider is None:
            raise HTTPException(status_code=503, detail="Provider is not configured")

        messages = [Message(role=item.role, content=item.content) for item in request.messages]
        options = GenerationOptions(max_tokens=request.max_tokens, temperature=request.temperature)
        cache_key = None
        if request.cache and request.temperature == 0 and request.messages[-1].role == "user":
            cache_key = make_cache_key(
                key_hash=trace.gateway_key_id,
                provider=provider_name,
                model=model,
                messages=messages,
                options=options,
            )
            try:
                cached_json = await store.get_cached(cache_key)
            except RedisError as exc:
                logger.warning("Redis cache read failed: %s", type(exc).__name__)
            else:
                if cached_json is not None:
                    try:
                        cached_response = ChatResponse.model_validate_json(cached_json)
                    except ValidationError:
                        logger.warning("Ignoring invalid cached chat response")
                    else:
                        trace.cache_hit = True
                        trace.actual_model = cached_response.model
                        trace.input_tokens = 0
                        trace.output_tokens = 0
                        trace.estimated_cost_usd = Decimal("0")
                        return cached_response.model_copy(update={
                            "request_id": str(trace.request_id),
                            "cache_hit": True,
                            "attempts": [],
                            "retry_count": 0,
                            "failure_reason": None,
                        })
        request_id = str(trace.request_id)
        try:
            async with asyncio.timeout(0.75):
                lease = await admission.acquire(trace.gateway_key_id, policy.deadline_seconds + 10)
        except (RedisError, TimeoutError) as exc:
            raise HTTPException(status_code=503, detail="Admission control unavailable") from exc
        if lease is None:
            raise HTTPException(status_code=429, detail="Provider concurrency limit exceeded", headers={"Retry-After": "1"})
        try:
            try:
                outcome = await generate_with_fallback(
                    initial_provider=provider_name,
                    alias=route,
                    providers=selected_providers,
                    messages=messages,
                    options=options,
                    policy=policy,
                    allow_fallback=request.provider is None,
                )
            except RouteFailure as exc:
                trace.final_provider = exc.final_provider
                trace.actual_model = route.models.get(exc.final_provider)
                trace.retry_count = exc.retry_count
                trace.failure_reason = exc.failure_reason
                trace.attempts = exc.attempts
                raise HTTPException(
                    status_code=504 if exc.failure_reason == "deadline_exceeded" else 502,
                    detail={
                        "message": (
                            "Request deadline exceeded"
                            if exc.failure_reason == "deadline_exceeded"
                            else "Provider request failed"
                        ),
                        "request_id": request_id,
                        "initial_provider": exc.initial_provider,
                        "final_provider": exc.final_provider,
                        "retry_count": exc.retry_count,
                        "failure_reason": exc.failure_reason,
                        "attempts": [
                            AttemptResponse(
                                provider=attempt.provider,
                                model=attempt.model,
                                failure_reason=attempt.failure_reason,
                            ).model_dump()
                            for attempt in exc.attempts
                        ],
                    },
                ) from exc
        finally:
            try:
                async with asyncio.timeout(0.75):
                    await admission.release(trace.gateway_key_id, lease)
            except (RedisError, TimeoutError) as exc:
                # The bounded lease expires even if release cannot reach Redis.
                logger.warning("Admission lease release failed: %s", type(exc).__name__)
        result = outcome.generation
        trace.final_provider = outcome.final_provider
        trace.actual_model = result.model
        trace.input_tokens = result.usage.input_tokens
        trace.output_tokens = result.usage.output_tokens
        trace.retry_count = outcome.retry_count
        trace.failure_reason = outcome.failure_reason
        trace.attempts = outcome.attempts
        trace.estimated_cost_usd = prices.estimate(
            outcome.final_provider,
            result.model,
            result.usage.input_tokens,
            result.usage.output_tokens,
        )
        response = ChatResponse(
            request_id=request_id,
            provider=outcome.final_provider,
            model=result.model,
            text=result.text,
            finish_reason=result.finish_reason,
            usage=ChatUsage(
                input_tokens=result.usage.input_tokens,
                output_tokens=result.usage.output_tokens,
            ),
            initial_provider=outcome.initial_provider,
            final_provider=outcome.final_provider,
            retry_count=outcome.retry_count,
            failure_reason=outcome.failure_reason,
            attempts=[
                AttemptResponse(
                    provider=attempt.provider,
                    model=attempt.model,
                    failure_reason=attempt.failure_reason,
                )
                for attempt in outcome.attempts
            ],
        )
        if cache_key is not None and outcome.retry_count == 0 and outcome.final_provider == provider_name:
            try:
                await store.put_cached(cache_key, response.model_dump_json())
            except RedisError as exc:
                logger.warning("Redis cache write failed: %s", type(exc).__name__)
        return response

    if deployment.static_dir is not None:
        app.mount("/", StaticFiles(directory=deployment.static_dir, html=True), name="dashboard")
    app.add_middleware(IngressGuard, store=admission, policy=safety, production=deployment.production)
    return app


app = create_app()
