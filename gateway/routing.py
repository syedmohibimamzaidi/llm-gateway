import asyncio
import math
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass

from gateway.config import ModelAlias
from gateway.providers.base import (
    FailureReason,
    Generation,
    GenerationOptions,
    Message,
    Provider,
    ProviderError,
)


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 2
    base_delay_seconds: float = 0.1
    deadline_seconds: float = 45.0

    def __post_init__(self) -> None:
        if isinstance(self.max_retries, bool) or not 0 <= self.max_retries <= 5:
            raise ValueError("max_retries must be between 0 and 5")
        if not math.isfinite(self.base_delay_seconds) or not 0 <= self.base_delay_seconds <= 5:
            raise ValueError("base_delay_seconds must be between 0 and 5")
        if not math.isfinite(self.deadline_seconds) or not 0 < self.deadline_seconds <= 60:
            raise ValueError("deadline_seconds must be greater than 0 and at most 60")

    @classmethod
    def from_env(cls) -> "RetryPolicy":
        return cls(
            max_retries=int(os.getenv("GATEWAY_MAX_RETRIES", "2")),
            base_delay_seconds=float(os.getenv("GATEWAY_RETRY_BASE_DELAY_SECONDS", "0.1")),
            deadline_seconds=float(os.getenv("GATEWAY_REQUEST_DEADLINE_SECONDS", "45")),
        )


@dataclass(frozen=True)
class Attempt:
    provider: str
    model: str
    failure_reason: FailureReason | None


@dataclass(frozen=True)
class RouteResult:
    generation: Generation
    initial_provider: str
    final_provider: str
    retry_count: int
    failure_reason: FailureReason | None
    attempts: tuple[Attempt, ...]


class RouteFailure(Exception):
    def __init__(
        self,
        *,
        initial_provider: str,
        final_provider: str,
        retry_count: int,
        failure_reason: FailureReason,
        attempts: tuple[Attempt, ...],
    ) -> None:
        super().__init__(failure_reason)
        self.initial_provider = initial_provider
        self.final_provider = final_provider
        self.retry_count = retry_count
        self.failure_reason = failure_reason
        self.attempts = attempts


async def generate_with_fallback(
    *,
    initial_provider: str,
    alias: ModelAlias,
    providers: Mapping[str, Provider],
    messages: Sequence[Message],
    options: GenerationOptions,
    policy: RetryPolicy,
    allow_fallback: bool = True,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> RouteResult:
    candidates = [initial_provider]
    secondary = "anthropic" if initial_provider == "openai" else "openai"
    if allow_fallback and secondary in alias.models and secondary in providers:
        candidates.append(secondary)

    attempts: list[Attempt] = []
    retry_count = 0
    failure_reason: FailureReason | None = None
    current_provider = initial_provider
    active_attempt: tuple[str, str] | None = None

    async def run_attempts() -> RouteResult:
        nonlocal retry_count, failure_reason, current_provider, active_attempt
        for provider_name in candidates:
            current_provider = provider_name
            model = alias.models[provider_name]
            provider = providers[provider_name]
            for retry_index in range(policy.max_retries + 1):
                if retry_index:
                    retry_count += 1
                active_attempt = (provider_name, model)
                try:
                    generation = await provider.generate(messages, model=model, options=options)
                except ProviderError as exc:
                    active_attempt = None
                    failure_reason = exc.reason
                    attempts.append(Attempt(provider_name, model, exc.reason))
                    if not exc.retryable:
                        raise RouteFailure(
                            initial_provider=initial_provider,
                            final_provider=provider_name,
                            retry_count=retry_count,
                            failure_reason=exc.reason,
                            attempts=tuple(attempts),
                        ) from exc
                    if retry_index < policy.max_retries:
                        delay = policy.base_delay_seconds * 2**retry_index
                        if delay:
                            await sleep(delay)
                        continue
                    break
                else:
                    active_attempt = None
                    attempts.append(Attempt(provider_name, model, None))
                    return RouteResult(
                        generation=generation,
                        initial_provider=initial_provider,
                        final_provider=provider_name,
                        retry_count=retry_count,
                        failure_reason=failure_reason,
                        attempts=tuple(attempts),
                    )

        raise RouteFailure(
            initial_provider=initial_provider,
            final_provider=candidates[-1],
            retry_count=retry_count,
            failure_reason=failure_reason or "provider_error",
            attempts=tuple(attempts),
        )

    deadline = asyncio.timeout(policy.deadline_seconds)
    try:
        async with deadline:
            return await run_attempts()
    except TimeoutError as exc:
        if not deadline.expired():
            raise
        if active_attempt is not None:
            attempts.append(Attempt(*active_attempt, "deadline_exceeded"))
        raise RouteFailure(
            initial_provider=initial_provider,
            final_provider=current_provider,
            retry_count=retry_count,
            failure_reason="deadline_exceeded",
            attempts=tuple(attempts),
        ) from exc
