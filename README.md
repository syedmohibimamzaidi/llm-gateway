# LLM Gateway — local MVP

An implementation of the project plan in `LLM_Gateway_MVP.txt`.

## Development

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
```

## API

Copy `.env.example` to `.env` and configure a gateway key, model aliases and provider credentials. Start the API with:

```sh
.venv/bin/uvicorn gateway.main:app --host 127.0.0.1 --port 8000
```

`gateway.main` loads `.env` before app creation and overrides stale exported values. `POST /v1/chat` accepts a model alias, messages, generation parameters and an optional strict provider override. Callers use a bearer gateway key; provider keys are used by the adapters. Tests inject fake providers and never make paid calls.

Transient timeouts, rate limits and server errors receive bounded exponential retries and compatible fallback. Explicit provider overrides stay on that provider. Authentication and invalid-request errors are not retried. The default provider-routing deadline is 45 seconds.
