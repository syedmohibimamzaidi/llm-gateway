import json
import os
from dataclasses import dataclass
from typing import Mapping


PROVIDER_NAMES = frozenset({"openai", "anthropic"})


@dataclass(frozen=True)
class ModelAlias:
    default_provider: str
    models: Mapping[str, str]


def load_model_aliases() -> dict[str, ModelAlias]:
    """Read provider/model routing without embedding live model names in code."""
    raw = os.getenv("GATEWAY_MODEL_ALIASES", "")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or not {"fast", "smart"} <= data.keys():
            raise ValueError("Both fast and smart aliases are required")
        aliases: dict[str, ModelAlias] = {}
        for alias, entry in data.items():
            if not isinstance(alias, str) or not isinstance(entry, dict):
                raise ValueError("Invalid model alias")
            default = entry["default_provider"]
            models = entry["models"]
            if (
                not isinstance(models, dict)
                or not models
                or any(name not in PROVIDER_NAMES or not isinstance(model, str) or not model.strip()
                       for name, model in models.items())
                or default not in models
            ):
                raise ValueError("Invalid provider/model mapping")
            aliases[alias] = ModelAlias(default_provider=default, models=models)
        return aliases
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("Invalid GATEWAY_MODEL_ALIASES configuration") from exc
