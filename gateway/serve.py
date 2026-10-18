"""Container entrypoint: honor Render PORT; never trust client-supplied proxy headers."""

import os
import sys

import uvicorn

from gateway.deployment import boolean, production_mode, validate_datastore_urls
from gateway.migrate import migrate


def main():
    port = int(os.getenv("PORT", "8000"))
    if not 1 <= port <= 65535:
        raise ValueError("Invalid PORT")
    if boolean("GATEWAY_RUN_MIGRATIONS", False):
        try:
            # Validate transport security before opening a migration connection.
            if production_mode():
                validate_datastore_urls(os.getenv("DATABASE_URL"), os.getenv("REDIS_URL"))
            migrate(os.getenv("DATABASE_URL", ""))
        except Exception as exc:
            print(f"Startup migration failed: {type(exc).__name__}", file=sys.stderr)
            raise SystemExit(1) from None
        print("Startup migrations applied", flush=True)
    uvicorn.run("gateway.main:app", host="0.0.0.0", port=port, proxy_headers=False)


if __name__ == "__main__":
    main()
