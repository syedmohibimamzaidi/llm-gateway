import asyncio

import pytest

from gateway.config import ModelAlias
from gateway.providers.base import Generation, GenerationOptions, Message, ProviderError, Usage
from gateway.routing import RetryPolicy, RouteFailure, generate_with_fallback


def test_exponential_backoff_is_bounded_by_retry_count() -> None:
    class EventuallySucceeds:
        name = "openai"

        def __init__(self) -> None:
            self.calls = 0

        async def generate(
            self, messages: list[Message], *, model: str, options: GenerationOptions
        ) -> Generation:
            self.calls += 1
            if self.calls <= 2:
                raise ProviderError("timeout")
            return Generation("ok", model, "stop", Usage(1, 1))

    async def run() -> None:
        provider = EventuallySucceeds()
        delays: list[float] = []

        async def record_delay(seconds: float) -> None:
            delays.append(seconds)

        result = await generate_with_fallback(
            initial_provider="openai",
            alias=ModelAlias("openai", {"openai": "configured-model"}),
            providers={"openai": provider},
            messages=[Message("user", "Hi")],
            options=GenerationOptions(),
            policy=RetryPolicy(max_retries=2, base_delay_seconds=0.25),
            sleep=record_delay,
        )
        assert provider.calls == 3
        assert result.retry_count == 2
        assert delays == [0.25, 0.5]

    asyncio.run(run())


@pytest.mark.parametrize("count", [-1, 6])
def test_retry_policy_rejects_unbounded_retry_counts(count: int) -> None:
    with pytest.raises(ValueError, match="max_retries"):
        RetryPolicy(max_retries=count)


def test_deadline_during_backoff_stops_retry_and_fallback() -> None:
    class AlwaysTimesOut:
        name = "openai"

        def __init__(self) -> None:
            self.calls = 0

        async def generate(
            self, messages: list[Message], *, model: str, options: GenerationOptions
        ) -> Generation:
            self.calls += 1
            raise ProviderError("timeout")

    async def run() -> None:
        primary = AlwaysTimesOut()
        secondary = AlwaysTimesOut()
        with pytest.raises(RouteFailure) as error:
            await generate_with_fallback(
                initial_provider="openai",
                alias=ModelAlias(
                    "openai", {"openai": "first-model", "anthropic": "second-model"}
                ),
                providers={"openai": primary, "anthropic": secondary},
                messages=[Message("user", "Hi")],
                options=GenerationOptions(),
                policy=RetryPolicy(
                    max_retries=2, base_delay_seconds=1, deadline_seconds=0.05
                ),
            )
        assert error.value.failure_reason == "deadline_exceeded"
        assert error.value.final_provider == "openai"
        assert error.value.retry_count == 0
        assert [(attempt.provider, attempt.failure_reason) for attempt in error.value.attempts] == [
            ("openai", "timeout")
        ]
        assert primary.calls == 1
        assert secondary.calls == 0

    asyncio.run(run())


@pytest.mark.parametrize("seconds", [0, 61, float("inf")])
def test_retry_policy_rejects_deadlines_outside_limit(seconds: float) -> None:
    with pytest.raises(ValueError, match="deadline_seconds"):
        RetryPolicy(deadline_seconds=seconds)
