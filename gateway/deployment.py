"""Explicit, fail-closed production configuration; no secret values in errors."""

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


def boolean(name: str, default: bool) -> bool:
    value = os.getenv(name, str(default)).lower()
    if value not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false")
    return value == "true"


def production_mode() -> bool:
    mode = os.getenv("GATEWAY_ENV", "development")
    if mode not in {"development", "test", "production"}:
        raise ValueError("Invalid GATEWAY_ENV")
    return mode == "production"


@dataclass(frozen=True)
class Deployment:
    production: bool
    paid_enabled: bool
    static_dir: Path | None

    @classmethod
    def from_env(cls):
        production = production_mode()
        static = os.getenv("GATEWAY_STATIC_DIR")
        return cls(production, boolean("GATEWAY_PAID_PROVIDERS_ENABLED", not production), Path(static) if static else None)

    def validate(self, *, key, aliases, providers, prices, database_url, redis_url):
        if not self.production:
            return
        if not 32 <= len(key) <= 256 or len(set(key)) < 8 or any(char.isspace() for char in key) or any(word in key.lower() for word in ("replace-with", "changeme", "example", "your-secret")):
            raise ValueError("Production requires a strong GATEWAY_API_KEY")
        if not aliases:
            raise ValueError("Production requires model aliases")
        if not self.static_dir or not (self.static_dir / "index.html").is_file() or not (self.static_dir / "assets").is_dir():
            raise ValueError("Production requires a built dashboard in GATEWAY_STATIC_DIR")
        if prices.version in {"unconfigured", "local-test-pricing-v1"} or any(
            (provider, model) not in prices.models for alias in aliases.values() for provider, model in alias.models.items()
        ):
            raise ValueError("Production requires complete non-fixture pricing")
        if self.paid_enabled:
            if any(alias.default_provider not in providers for alias in aliases.values()):
                raise ValueError("Production requires credentials for every default provider")
            for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
                value = os.getenv(name, "")
                if value and (len(value) < 20 or "replace-with" in value.lower() or value != value.strip()):
                    raise ValueError("Invalid production provider credential")
        validate_datastore_urls(database_url, redis_url)


def validate_datastore_urls(database_url, redis_url):
    """Validate production transports before either runtime or migrations connect."""
    network = os.getenv("GATEWAY_DATASTORE_NETWORK", "tls")
    if network not in {"tls", "render-private"}:
        raise ValueError("Invalid GATEWAY_DATASTORE_NETWORK")
    for kind, url in (("database", database_url), ("redis", redis_url)):
        try:
            parsed = urlsplit(url or "")
            if not parsed.hostname or parsed.hostname in {"localhost", "127.0.0.1", "0.0.0.0", "::1"}:
                raise ValueError
            if parsed.port is not None and not 1 <= parsed.port <= 65535:
                raise ValueError
            if kind == "database":
                if parsed.scheme not in {"postgres", "postgresql"} or not parsed.username or not parsed.password or parsed.password == "gateway-dev-password" or not parsed.path.strip("/"):
                    raise ValueError
            elif parsed.scheme not in {"redis", "rediss"}:
                raise ValueError
            query = parse_qs(parsed.query)
            if kind == "redis" and parsed.scheme == "rediss":
                if query.get("ssl_cert_reqs", ["required"]) != ["required"] or query.get("ssl_check_hostname", ["true"]) != ["true"]:
                    raise ValueError
            if network == "render-private":
                # Explicit opt-in to same-region Render private networking.
                prefix = "dpg-" if kind == "database" else "red-"
                if not parsed.hostname.startswith(prefix) or "." in parsed.hostname:
                    raise ValueError
            elif kind == "database":
                if parse_qs(parsed.query).get("sslmode") != ["verify-full"]:
                    raise ValueError
            elif parsed.scheme != "rediss":
                raise ValueError
        except (ValueError, TypeError):
            raise ValueError(f"Invalid production {kind} connection configuration") from None
