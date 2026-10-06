from typing import Sequence

import httpx

from gateway.providers.base import Generation, GenerationOptions, Message, ProviderError, Usage


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, *, api_key: str, client: httpx.AsyncClient | None = None) -> None:
        self.api_key = api_key
        self.client = client or httpx.AsyncClient(timeout=30.0)
        self._owns_client = client is None

    async def generate(
        self, messages: Sequence[Message], *, model: str, options: GenerationOptions
    ) -> Generation:
        system = "\n\n".join(message.content for message in messages if message.role == "system")
        body: dict[str, object] = {
            "model": model,
            "max_tokens": options.max_tokens,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in messages
                if message.role != "system"
            ],
        }
        if system:
            body["system"] = system
        if options.temperature is not None:
            body["temperature"] = options.temperature
        try:
            response = await self.client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                },
                json=body,
            )
            response.raise_for_status()
            payload = response.json()
            blocks = payload["content"]
            if not isinstance(blocks, list):
                raise ValueError("Expected content blocks")
            text_blocks = [block["text"] for block in blocks if block["type"] == "text"]
            if not text_blocks or not all(isinstance(text, str) for text in text_blocks):
                raise ValueError("Expected text content")
            usage = payload["usage"]
            return Generation(
                text="".join(text_blocks),
                model=payload["model"],
                finish_reason={
                    "end_turn": "stop",
                    "stop_sequence": "stop",
                    "max_tokens": "length",
                }.get(payload.get("stop_reason"), payload.get("stop_reason")),
                usage=Usage(
                    input_tokens=usage["input_tokens"],
                    output_tokens=usage["output_tokens"],
                ),
            )
        except httpx.HTTPStatusError as exc:
            raise ProviderError.from_status(exc.response.status_code) from exc
        except httpx.TimeoutException as exc:
            raise ProviderError("timeout") from exc
        except (httpx.RequestError, ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError("provider_error") from exc

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()
