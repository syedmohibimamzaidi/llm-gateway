import asyncio
import hashlib
import hmac
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated, Awaitable, Literal, Mapping, TypeVar
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from gateway.config import ModelAlias, load_model_aliases
from gateway.providers.anthropic import AnthropicProvider
from gateway.providers.base import FailureReason, GenerationOptions, Message, Provider
from gateway.providers.openai import OpenAIProvider
from gateway.routing import RetryPolicy, RouteFailure, generate_with_fallback


logger = logging.getLogger(__name__)
T = TypeVar("T")


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str = Field(pattern="^(system|user|assistant)$")
    content: str = Field(min_length=1)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1)
    messages: list[ChatMessage] = Field(min_length=1)
    max_tokens: int = Field(default=1024, ge=1)
    temperature: float | None = Field(default=None, ge=0, le=1)
    provider: Literal["openai", "anthropic"] | None = None

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


@dataclass
class RequestContext:
    request_id: UUID
    gateway_key_id: str | None = None


def create_app(
    *,
    gateway_api_key: str | None = None,
    providers: Mapping[str, Provider] | None = None,
    model_aliases: Mapping[str, ModelAlias] | None = None,
    retry_policy: RetryPolicy | None = None,
) -> FastAPI:
    configured_key = gateway_api_key if gateway_api_key is not None else os.getenv("GATEWAY_API_KEY", "")
    configured_key_hash = hashlib.sha256(configured_key.encode()).digest() if configured_key else None
    aliases = model_aliases if model_aliases is not None else load_model_aliases()
    policy = retry_policy if retry_policy is not None else RetryPolicy.from_env()
    selected_providers = dict(providers) if providers is not None else {}
    if providers is None:
        if provider_key := os.getenv("OPENAI_API_KEY"):
            selected_providers["openai"] = OpenAIProvider(api_key=provider_key)
        if provider_key := os.getenv("ANTHROPIC_API_KEY"):
            selected_providers["anthropic"] = AnthropicProvider(api_key=provider_key)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            yield
        finally:
            for provider in selected_providers.values():
                if isinstance(provider, (OpenAIProvider, AnthropicProvider)):
                    await provider.aclose()

    app = FastAPI(title="LLM Gateway", lifespan=lifespan)

    @app.middleware("http")
    async def identify_chat_request(http_request: Request, call_next):
        if http_request.url.path != "/v1/chat" or http_request.method != "POST":
            return await call_next(http_request)
        trace = RequestContext(request_id=uuid4())
        http_request.state.telemetry = trace
        try:
            trace.gateway_key_id = await authenticate_gateway_key(http_request.headers.get("authorization"))
        except HTTPException as exc:
            response = JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers)
        else:
            response = await call_next(http_request)
        response.headers["X-Request-ID"] = str(trace.request_id)
        return response

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    async def authenticate_gateway_key(authorization: Annotated[str | None, Header()] = None) -> str:
        if configured_key_hash is None:
            raise HTTPException(status_code=503, detail="Gateway API key is not configured")
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token or token != token.strip() or " " in token:
            raise HTTPException(status_code=401, detail="Invalid gateway API key")
        supplied_hash = hashlib.sha256(token.encode()).digest()
        if hmac.compare_digest(supplied_hash, configured_key_hash):
            return configured_key_hash.hex()
        raise HTTPException(status_code=401, detail="Invalid gateway API key")

    @app.post("/v1/chat", response_model=ChatResponse)
    async def chat(
        request: ChatRequest,
        http_request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ) -> ChatResponse:
        trace: RequestContext = http_request.state.telemetry

        assert trace.gateway_key_id is not None
        if not aliases:
            raise HTTPException(status_code=503, detail="Model aliases are not configured")
        route = aliases.get(request.model)
        if route is None:
            raise HTTPException(status_code=422, detail="Unknown model alias")
        provider_name = request.provider or route.default_provider
        model = route.models.get(provider_name)
        if model is None:
            raise HTTPException(status_code=422, detail="Provider is not configured for this alias")



        selected_provider = selected_providers.get(provider_name)
        if selected_provider is None:
            raise HTTPException(status_code=503, detail="Provider is not configured")

        messages = [Message(role=item.role, content=item.content) for item in request.messages]
        options = GenerationOptions(max_tokens=request.max_tokens, temperature=request.temperature)
        request_id = str(trace.request_id)
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
        result = outcome.generation








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
        return response

    return app


app = create_app()
