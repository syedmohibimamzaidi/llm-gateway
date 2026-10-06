import pytest

from gateway.config import load_model_aliases


def test_aliases_load_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "GATEWAY_MODEL_ALIASES",
        '{"fast":{"default_provider":"openai","models":{"openai":"fast-one","anthropic":"fast-two"}},'
        '"smart":{"default_provider":"anthropic","models":{"anthropic":"smart-two"}}}',
    )
    aliases = load_model_aliases()
    assert aliases["fast"].models["anthropic"] == "fast-two"
    assert aliases["smart"].default_provider == "anthropic"


@pytest.mark.parametrize("value", ["not json", "{}", '{"fast":{"default_provider":"openai","models":{}}}'])
def test_invalid_alias_config_fails_early(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("GATEWAY_MODEL_ALIASES", value)
    with pytest.raises(ValueError, match="Invalid GATEWAY_MODEL_ALIASES"):
        load_model_aliases()
