import asyncio
import json

import httpx
import pytest

from gateway.providers.anthropic import AnthropicProvider
from gateway.providers.base import GenerationOptions, Message, ProviderError


def test_anthropic_adapter_moves_system_and_normalizes_response() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://api.anthropic.com/v1/messages"
        assert request.headers["x-api-key"] == "provider-secret"
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert json.loads(request.content) == {
            "model": "selected-model",
            "max_tokens": 64,
            "temperature": 0.5,
            "system": "Be concise.",
            "messages": [{"role": "user", "content": "Hi"}],
        }
        return httpx.Response(
            200,
            json={
                "model": "resolved-model",
                "content": [
                    {"type": "text", "text": "Hello"},
                    {"type": "text", "text": "!"},
                ],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 4, "output_tokens": 2},
            },
        )

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider = AnthropicProvider(api_key="provider-secret", client=client)
            result = await provider.generate(
                [Message(role="system", content="Be concise."), Message(role="user", content="Hi")],
                model="selected-model",
                options=GenerationOptions(max_tokens=64, temperature=0.5),
            )
        assert result.text == "Hello!"
        assert result.model == "resolved-model"
        assert result.finish_reason == "stop"
        assert result.usage.input_tokens == 4
        assert result.usage.output_tokens == 2

    asyncio.run(run())


@pytest.mark.parametrize(
    ("status", "reason"),
    [(400, "invalid_request"), (401, "authentication"), (403, "authentication"),
     (429, "rate_limit"), (503, "server_error")],
)
def test_anthropic_adapter_classifies_http_errors(status: int, reason: str) -> None:
    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(status))
        ) as client:
            provider = AnthropicProvider(api_key="provider-secret", client=client)
            with pytest.raises(ProviderError) as error:
                await provider.generate(
                    [Message(role="user", content="Hi")],
                    model="selected-model",
                    options=GenerationOptions(),
                )
            assert error.value.reason == reason
            assert error.value.retryable == (reason in {"rate_limit", "server_error"})

    asyncio.run(run())


def test_anthropic_adapter_classifies_timeout() -> None:
    def time_out(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated timeout", request=request)

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(time_out)) as client:
            provider = AnthropicProvider(api_key="provider-secret", client=client)
            with pytest.raises(ProviderError) as error:
                await provider.generate(
                    [Message(role="user", content="Hi")],
                    model="selected-model",
                    options=GenerationOptions(),
                )
            assert error.value.reason == "timeout"
            assert error.value.retryable

    asyncio.run(run())
