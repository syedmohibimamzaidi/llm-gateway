import json
import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class TokenPrice:
    input_per_million_usd: Decimal
    output_per_million_usd: Decimal


@dataclass(frozen=True)
class PriceBook:
    version: str
    models: Mapping[tuple[str, str], TokenPrice]

    @classmethod
    def from_env(cls) -> "PriceBook":
        path = os.getenv("GATEWAY_PRICE_CONFIG_PATH")
        if not path:
            return cls(version="unconfigured", models={})
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            version = data["version"]
            if not isinstance(version, str) or not version.strip():
                raise ValueError("A price version is required")
            prices: dict[tuple[str, str], TokenPrice] = {}
            for provider, models in data["models"].items():
                if provider not in {"openai", "anthropic"} or not isinstance(models, dict):
                    raise ValueError("Invalid provider prices")
                for model, rates in models.items():
                    if not isinstance(model, str) or not model.strip():
                        raise ValueError("Invalid model price")
                    input_rate = Decimal(str(rates["input_per_million_usd"]))
                    output_rate = Decimal(str(rates["output_per_million_usd"]))
                    if any(not rate.is_finite() or rate < 0 for rate in (input_rate, output_rate)):
                        raise ValueError("Prices must be finite and nonnegative")
                    prices[(provider, model)] = TokenPrice(input_rate, output_rate)
            return cls(version=version, models=prices)
        except (OSError, json.JSONDecodeError, KeyError, AttributeError, TypeError,
                InvalidOperation, ValueError) as exc:
            raise ValueError("Invalid GATEWAY_PRICE_CONFIG_PATH configuration") from exc

    def estimate(
        self, provider: str, model: str, input_tokens: int, output_tokens: int
    ) -> Decimal | None:
        price = self.models.get((provider, model))
        if price is None:
            return None
        cost = (
            Decimal(input_tokens) * price.input_per_million_usd
            + Decimal(output_tokens) * price.output_per_million_usd
        ) / Decimal(1_000_000)
        return cost.quantize(Decimal("0.000000000001"))
