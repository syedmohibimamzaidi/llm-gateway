import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest
import yaml
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.config import ModelAlias
from gateway.deployment import Deployment
from gateway.pricing import PriceBook
from gateway.safety import MemorySafetyStore, SafetyPolicy


ROOT = Path(__file__).resolve().parents[1]
KEY = "private-demo-configuration-fixture-1234567890"


class Store:
    async def allow(self, key):
        return True
    async def get_cached(self, key):
        return None
    async def put_cached(self, key, value):
        pass


class Telemetry:
    async def record(self, event):
        pass


class Provider:
    name = "openai"
    calls = 0
    async def generate(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("Paid provider must remain disabled")


class Analytics:
    async def summary(self, *args):
        return {"request_count": 0}


@pytest.fixture
def production(monkeypatch, tmp_path):
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text('<html><script src="/assets/main.js"></script></html>')
    (tmp_path / "assets/main.js").write_text("// static fixture")
    values = {
        "GATEWAY_ENV": "production", "GATEWAY_API_KEY": KEY,
        "DATABASE_URL": "postgresql://owner:fixture-secret-123@dpg-fixture/gateway",
        "REDIS_URL": "redis://red-fixture:6379/0", "GATEWAY_DATASTORE_NETWORK": "render-private",
        "GATEWAY_STATIC_DIR": str(tmp_path), "GATEWAY_PRICE_CONFIG_PATH": str(ROOT / "prices.live.json"),
    }
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GATEWAY_PAID_PROVIDERS_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return values


def app(provider=None):
    provider = provider or Provider()
    return create_app(
        providers={"openai": provider},
        model_aliases={name: ModelAlias("openai", {"openai": "gpt-4.1-mini-2025-04-14"}) for name in ("fast", "smart")},
        redis_store=Store(), safety_store=MemorySafetyStore(SafetyPolicy()),
        telemetry_repository=Telemetry(), analytics_repository=Analytics(),
    )


def test_production_serves_static_and_authenticated_api_on_one_origin(production):
    provider = Provider()
    with TestClient(app(provider)) as client:
        index = client.get("/")
        assert index.status_code == 200 and '/assets/main.js' in index.text
        assert "frame-ancestors 'none'" in index.headers["Content-Security-Policy"]
        assert client.get("/assets/main.js").status_code == 200
        assert client.get("/analytics/summary").status_code == 401
        assert client.get("/analytics/summary", headers={"Authorization": "Bearer " + KEY}).json()["request_count"] == 0
        assert client.get("/requests").status_code == 401
        assert client.get("/api-keys").status_code == 401
        for path in ("/docs", "/openapi.json", "/v1/nonexistent", "/assets/no-file.js", "/%2e%2e/prices.live.json"):
            assert client.get(path).status_code == 404
        response = client.post("/v1/chat", headers={"Authorization": "Bearer " + KEY}, json={"model": "fast", "messages": [{"role": "user", "content": "fixture"}], "max_tokens": 24})
        assert response.status_code == 503 and response.json()["detail"] == "Provider access is disabled"
        assert client.get("/health").status_code == 200
        assert client.get("/readyz").status_code == 503  # No real datastores injected.
    assert provider.calls == 0


@pytest.mark.parametrize("name,value", [
    ("GATEWAY_API_KEY", "replace-with-a-long-random-secret"),
    ("GATEWAY_API_KEY", "short"),
    ("GATEWAY_API_KEY", "x" * 40),
    ("GATEWAY_API_KEY", KEY + " with spaces"),
    ("DATABASE_URL", "postgresql://gateway:gateway-dev-password@dpg-fixture/gateway"),
    ("DATABASE_URL", "postgresql://owner:fixture-secret-123@localhost/gateway"),
    ("REDIS_URL", "redis://public.example.com:6379/0"),
    ("GATEWAY_STATIC_DIR", "/nonexistent/static"),
    ("GATEWAY_PRICE_CONFIG_PATH", str(ROOT / "prices.example.json")),
    ("GATEWAY_PAID_PROVIDERS_ENABLED", "yes"),
    ("GATEWAY_DATASTORE_NETWORK", "anything"),
])
def test_production_rejects_unsafe_configuration_without_echoing_values(production, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError) as exc:
        app()
    assert "fixture-secret-123" not in str(exc.value)
    assert "gateway-dev-password" not in str(exc.value)


def test_production_tls_urls_require_certificate_verification(production, monkeypatch):
    monkeypatch.setenv("GATEWAY_DATASTORE_NETWORK", "tls")
    monkeypatch.setenv("DATABASE_URL", "postgresql://owner:fixture-secret-123@db.example.com/gateway?sslmode=verify-full")
    monkeypatch.setenv("REDIS_URL", "rediss://default:fixture@redis.example.com:6380/0")
    app()
    monkeypatch.setenv("REDIS_URL", "rediss://default:fixture@redis.example.com:6380/0?ssl_cert_reqs=none")
    with pytest.raises(ValueError, match="redis connection"):
        app()
    monkeypatch.setenv("REDIS_URL", "rediss://default:fixture@redis.example.com:6380/0?ssl_check_hostname=false")
    with pytest.raises(ValueError, match="redis connection"):
        app()
    monkeypatch.setenv("REDIS_URL", "rediss://default:fixture@redis.example.com:6380/0")
    monkeypatch.setenv("DATABASE_URL", "postgresql://owner:fixture-secret-123@db.example.com/gateway?sslmode=require")
    with pytest.raises(ValueError, match="database connection"):
        app()
    monkeypatch.setenv("DATABASE_URL", "postgresql://owner:fixture-secret-123@db.example.com/gateway?sslmode=verify-full")
    monkeypatch.setenv("REDIS_URL", "redis://redis.example.com:6379/0")
    with pytest.raises(ValueError, match="redis connection"):
        app()


def test_enabling_providers_requires_alias_defaults_credentials_and_prices(production, monkeypatch):
    monkeypatch.setenv("GATEWAY_PAID_PROVIDERS_ENABLED", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "replace-with-your-provider-key")
    with pytest.raises(ValueError, match="provider credential"):
        app()
    monkeypatch.delenv("OPENAI_API_KEY")
    with pytest.raises(ValueError, match="credentials for every default"):
        create_app(providers={}, model_aliases={"fast": ModelAlias("openai", {"openai": "gpt-4.1-mini-2025-04-14"})})
    with pytest.raises(ValueError, match="complete non-fixture pricing"):
        create_app(providers={}, model_aliases={"fast": ModelAlias("openai", {"openai": "unknown-model"})})


def test_production_entrypoint_never_loads_local_dotenv(production, tmp_path):
    secret_file = tmp_path / ".env"
    secret_file.write_text("GATEWAY_API_KEY=wrong-local-fixture\nGATEWAY_MODEL_ALIASES=invalid-json\n")
    environment = {**os.environ, "GATEWAY_ENV_FILE": str(secret_file), "GATEWAY_MODEL_ALIASES": json.dumps({n: {"default_provider": "openai", "models": {"openai": "gpt-4.1-mini-2025-04-14"}} for n in ("fast", "smart")})}
    result = subprocess.run([sys.executable, "-c", "import os; import gateway.main; assert os.environ['GATEWAY_API_KEY'] != 'wrong-local-fixture'"], cwd=ROOT, env=environment, capture_output=True, timeout=10)
    assert result.returncode == 0, 'Production entrypoint failed (output withheld to protect environment)'


def test_render_and_ci_configuration_are_private_by_default():
    blueprint = yaml.safe_load((ROOT / "render.yaml").read_text())
    service, = blueprint["services"]
    assert service["runtime"] == "docker" and service["healthCheckPath"] == "/readyz"
    assert service["autoDeployTrigger"] in {"off", False}  # YAML 1.1 treats off as false.
    assert service["preDeployCommand"] == "python -m gateway.migrate"
    variables = {v["key"]: v for v in service["envVars"]}
    assert variables["GATEWAY_PAID_PROVIDERS_ENABLED"]["value"] == "false"
    for name in ("GATEWAY_API_KEY", "DATABASE_URL", "REDIS_URL", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        assert variables[name] == {"key": name, "sync": False}
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    assert workflow["permissions"] == {"contents": "read"}
    steps = str(workflow["jobs"]["verify"]["steps"])
    assert "pytest" in steps and "npm audit" in steps and "docker build" in steps
    assert "gateway.migrate" in steps and "/readyz" in steps
    lock = json.loads((ROOT / "dashboard/package-lock.json").read_text())
    assert lock["packages"]["node_modules/source-map-js"]["version"] == "1.2.2"


def test_container_entrypoint_honors_port_and_rejects_proxy_spoofing(monkeypatch):
    from gateway import serve
    seen = {}
    monkeypatch.setenv("PORT", "10000")
    monkeypatch.setattr(serve.uvicorn, "run", lambda app, **kwargs: seen.update({"app": app, **kwargs}))
    serve.main()
    assert seen["port"] == 10000 and seen["host"] == "0.0.0.0"
    assert seen["proxy_headers"] is False
