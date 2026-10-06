import json
import os
import subprocess
import sys
from pathlib import Path

from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parents[1]


def test_startup_env_file_overrides_stale_exported_configuration(tmp_path) -> None:
    aliases = {
        "fast": {"default_provider": "openai", "models": {"openai": "file-model"}},
        "smart": {"default_provider": "openai", "models": {"openai": "file-model"}},
    }
    env_file = tmp_path / ".env"
    env_file.write_text(
        "GATEWAY_API_KEY=file-fixture-key\n"
        "GATEWAY_MODEL_ALIASES='" + json.dumps(aliases) + "'\n"
        "GATEWAY_PRICE_CONFIG_PATH=\n"
        "DATABASE_URL=\n"
        "OPENAI_API_KEY=\n"
        "ANTHROPIC_API_KEY=\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment.update({
        "GATEWAY_ENV_FILE": str(env_file),
        "GATEWAY_API_KEY": "stale-fixture-key",
        "GATEWAY_MODEL_ALIASES": "stale-invalid-aliases",
        "GATEWAY_PRICE_CONFIG_PATH": "missing-price-file",
        "DATABASE_URL": "postgresql://stale-fixture.invalid/gateway",
    })
    result = subprocess.run(
        [sys.executable, "-c", (
            "import os; import gateway.main; "
            "from gateway.config import load_model_aliases; "
            "assert os.environ['GATEWAY_API_KEY'] == 'file-fixture-key'; "
            "assert load_model_aliases()['fast'].models['openai'] == 'file-model'; "
            "assert gateway.main.app.title == 'LLM Gateway'"
        )],
        cwd=ROOT, env=environment, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr


