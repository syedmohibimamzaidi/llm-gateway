"""Local and container entrypoint with predictable configuration precedence."""

import os

from dotenv import load_dotenv

# An explicit local .env overrides stale exported values. Containers without the
# file retain the environment injected by Compose.
if os.getenv("GATEWAY_ENV") != "production":
    load_dotenv(os.getenv("GATEWAY_ENV_FILE", ".env"), override=True)

from gateway.app import app  # noqa: E402
