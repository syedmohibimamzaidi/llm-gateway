"""Container entrypoint: honor Render PORT; never trust client-supplied proxy headers."""

import os

import uvicorn


def main():
    port = int(os.getenv("PORT", "8000"))
    if not 1 <= port <= 65535:
        raise ValueError("Invalid PORT")
    uvicorn.run("gateway.main:app", host="0.0.0.0", port=port, proxy_headers=False)


if __name__ == "__main__":
    main()
