from dataclasses import dataclass
from typing import Literal, Protocol, Sequence


@dataclass(frozen=True)
class Message:
    role: str
    content: str


@dataclass(frozen=True)
class GenerationOptions:
    max_tokens: int = 1024
    temperature: float | None = None


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class Generation:
    text: str
    model: str
    finish_reason: str | None
    usage: Usage


FailureReason = Literal[
    "timeout", "rate_limit", "server_error", "invalid_request", "authentication", "provider_error",
    "deadline_exceeded",
]


class ProviderError(Exception):
    """A classified upstream failure. Only transient reasons may be retried."""

    def __init__(self, reason: FailureReason, *, status_code: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code

    @property
    def retryable(self) -> bool:
        return self.reason in {"timeout", "rate_limit", "server_error"}

    @classmethod
    def from_status(cls, status_code: int) -> "ProviderError":
        if status_code == 429:
            reason: FailureReason = "rate_limit"
        elif 500 <= status_code <= 599:
            reason = "server_error"
        elif status_code in {401, 403}:
            reason = "authentication"
        elif 400 <= status_code <= 499:
            reason = "invalid_request"
        else:
            reason = "provider_error"
        return cls(reason, status_code=status_code)


class Provider(Protocol):
    name: str

    async def generate(
        self, messages: Sequence[Message], *, model: str, options: GenerationOptions
    ) -> Generation: ...
