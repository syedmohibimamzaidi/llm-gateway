FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY gateway ./gateway
COPY prices.example.json ./prices.json
COPY prices.live.json ./prices.live.json
RUN pip install --no-cache-dir .

EXPOSE 8000
CMD ["uvicorn", "gateway.main:app", "--host", "0.0.0.0", "--port", "8000"]
