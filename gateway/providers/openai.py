from typing import Sequence

import httpx

from gateway.providers.base import Generation, GenerationOptions, Message, ProviderError, Usage


class OpenAIProvider:
    name = "openai"

    def __init__(
        self,
        *,
        api_key: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key
        self.client = client or httpx.AsyncClient(timeout=30.0)
        self._owns_client = client is None

    async def generate(
        self, messages: Sequence[Message], *, model: str, options: GenerationOptions
    ) -> Generation:
        try:
            body: dict[str, object] = {
                "model": model,
                "messages": [
                    {"role": message.role, "content": message.content}
                    for message in messages
                ],
                "max_completion_tokens": options.max_tokens,
            }
            if options.temperature is not None:
                body["temperature"] = options.temperature
            response = await self.client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=body,
            )
            response.raise_for_status()
            payload = response.json()
            choice = payload["choices"][0]
            content = choice["message"]["content"]
            usage = payload["usage"]
            if not isinstance(content, str):
                raise ValueError("Expected text content")
            return Generation(
                text=content,
                model=payload["model"],
                finish_reason=choice.get("finish_reason"),
                usage=Usage(
                    input_tokens=usage["prompt_tokens"],
                    output_tokens=usage["completion_tokens"],
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
