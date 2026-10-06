import asyncio
import json

import httpx
import pytest

from gateway.providers.base import GenerationOptions, Message, ProviderError
from gateway.providers.openai import OpenAIProvider


def test_openai_adapter_uses_selected_model_and_options() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://api.openai.com/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer provider-secret"
        assert json.loads(request.content) == {
            "model": "selected-model",
            "messages": [{"role": "user", "content": "Hi"}],
            "max_completion_tokens": 64,
            "temperature": 0.5,
        }
        return httpx.Response(
            200,
            json={
                "model": "resolved-model",
                "choices": [{"message": {"content": "Hello!"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            },
        )

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider = OpenAIProvider(api_key="provider-secret", client=client)
            result = await provider.generate(
                [Message(role="user", content="Hi")],
                model="selected-model",
                options=GenerationOptions(max_tokens=64, temperature=0.5),
            )
        assert result.text == "Hello!"
        assert result.model == "resolved-model"
        assert result.finish_reason == "stop"
        assert result.usage.input_tokens == 3
        assert result.usage.output_tokens == 2

    asyncio.run(run())


@pytest.mark.parametrize(
    ("status", "reason"),
    [(400, "invalid_request"), (401, "authentication"), (403, "authentication"),
     (429, "rate_limit"), (500, "server_error")],
)
def test_openai_adapter_classifies_http_errors(status: int, reason: str) -> None:
    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(status))
        ) as client:
            provider = OpenAIProvider(api_key="provider-secret", client=client)
            with pytest.raises(ProviderError) as error:
                await provider.generate(
                    [Message(role="user", content="Hi")],
                    model="selected-model",
                    options=GenerationOptions(),
                )
            assert error.value.reason == reason
            assert error.value.retryable == (reason in {"rate_limit", "server_error"})

    asyncio.run(run())


def test_openai_adapter_classifies_timeout() -> None:
    def time_out(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated timeout", request=request)

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(time_out)) as client:
            provider = OpenAIProvider(api_key="provider-secret", client=client)
            with pytest.raises(ProviderError) as error:
                await provider.generate(
                    [Message(role="user", content="Hi")],
                    model="selected-model",
                    options=GenerationOptions(),
                )
            assert error.value.reason == "timeout"
            assert error.value.retryable

    asyncio.run(run())
