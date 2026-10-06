import asyncio

from fastapi.testclient import TestClient

from gateway.app import create_app as build_app
from gateway.config import ModelAlias
from gateway.providers.base import FailureReason, Generation, GenerationOptions, Message, ProviderError, Usage


class UnlimitedStore:
    async def allow(self, key_hash: str) -> bool:
        return True

    async def get_cached(self, cache_key: str) -> str | None:
        return None

    async def put_cached(self, cache_key: str, response_json: str) -> None:
        pass


def create_app(**kwargs):
    return build_app(**kwargs)


class FakeProvider:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[tuple[list[Message], str, GenerationOptions]] = []
        self.failures: list[FailureReason] = []

    async def generate(
        self, messages: list[Message], *, model: str, options: GenerationOptions
    ) -> Generation:
        self.calls.append((list(messages), model, options))
        if self.failures:
            raise ProviderError(self.failures.pop(0))
        return Generation(
            text="Hello!",
            model=model,
            finish_reason="stop",
            usage=Usage(input_tokens=3, output_tokens=2),
        )


ALIASES = {
    "fast": ModelAlias(
        default_provider="openai",
        models={"openai": "openai-fast", "anthropic": "anthropic-fast"},
    ),
    "smart": ModelAlias(
        default_provider="anthropic",
        models={"openai": "openai-smart", "anthropic": "anthropic-smart"},
    ),
}
HEADERS = {"Authorization": "Bearer secret"}
BODY = {"model": "fast", "messages": [{"role": "user", "content": "Hi"}]}


def test_chat_requires_gateway_key() -> None:
    provider = FakeProvider("openai")
    with TestClient(create_app(
        gateway_api_key="secret", providers={"openai": provider}, model_aliases=ALIASES
    )) as client:
        assert client.post("/v1/chat", json=BODY).status_code == 401
        assert client.post(
            "/v1/chat", json=BODY, headers={"Authorization": "Bearer wrong"}
        ).status_code == 401
    assert provider.calls == []


def test_alias_defaults_and_provider_override_share_response_contract() -> None:
    openai = FakeProvider("openai")
    anthropic = FakeProvider("anthropic")
    with TestClient(create_app(
        gateway_api_key="secret",
        providers={"openai": openai, "anthropic": anthropic},
        model_aliases=ALIASES,
    )) as client:
        fast = client.post("/v1/chat", json=BODY, headers=HEADERS)
        smart = client.post(
            "/v1/chat", json={**BODY, "model": "smart"}, headers=HEADERS
        )
        override = client.post(
            "/v1/chat",
            json={**BODY, "provider": "anthropic", "max_tokens": 64, "temperature": 0.5},
            headers=HEADERS,
        )

    for response in (fast, smart, override):
        assert response.status_code == 200
        assert response.json()["text"] == "Hello!"
        assert response.json()["finish_reason"] == "stop"
        assert response.json()["usage"] == {"input_tokens": 3, "output_tokens": 2}
        assert response.json()["request_id"]
    assert (fast.json()["provider"], fast.json()["model"]) == ("openai", "openai-fast")
    assert (smart.json()["provider"], smart.json()["model"]) == ("anthropic", "anthropic-smart")
    assert (override.json()["provider"], override.json()["model"]) == ("anthropic", "anthropic-fast")
    assert fast.json()["initial_provider"] == "openai"
    assert fast.json()["final_provider"] == "openai"
    assert fast.json()["retry_count"] == 0
    assert fast.json()["failure_reason"] is None
    assert fast.json()["attempts"] == [
        {"provider": "openai", "model": "openai-fast", "failure_reason": None}
    ]
    assert openai.calls == [
        ([Message(role="user", content="Hi")], "openai-fast", GenerationOptions())
    ]
    assert anthropic.calls[1] == (
        [Message(role="user", content="Hi")],
        "anthropic-fast",
        GenerationOptions(max_tokens=64, temperature=0.5),
    )


def test_invalid_input_and_unavailable_route_do_not_call_provider() -> None:
    provider = FakeProvider("openai")
    with TestClient(create_app(
        gateway_api_key="secret", providers={"openai": provider}, model_aliases=ALIASES
    )) as client:
        for body in (
            {**BODY, "messages": []},
            {**BODY, "max_tokens": 0},
            {**BODY, "temperature": 1.5},
            {**BODY, "model": "unknown"},
            {**BODY, "provider": "other"},
            {**BODY, "messages": [{"role": "system", "content": "Hi"}]},
        ):
            assert client.post("/v1/chat", json=body, headers=HEADERS).status_code == 422
        assert client.post(
            "/v1/chat", json={**BODY, "model": "smart"}, headers=HEADERS
        ).status_code == 503
    assert provider.calls == []


def test_provider_failure_is_a_bad_gateway_response() -> None:
    provider = FakeProvider("openai")
    provider.failures = ["provider_error"]
    with TestClient(create_app(
        gateway_api_key="secret", providers={"openai": provider}, model_aliases=ALIASES
    )) as client:
        response = client.post("/v1/chat", json=BODY, headers=HEADERS)
    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["message"] == "Provider request failed"
    assert detail["initial_provider"] == "openai"
    assert detail["final_provider"] == "openai"
    assert detail["retry_count"] == 0
    assert detail["failure_reason"] == "provider_error"
    assert detail["request_id"]
    assert len(provider.calls) == 1














def test_health() -> None:
    with TestClient(create_app(
        gateway_api_key="secret", providers={}, model_aliases={}
    )) as client:
        assert client.get("/health").json() == {"status": "ok"}
