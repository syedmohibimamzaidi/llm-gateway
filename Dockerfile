FROM node:22-alpine AS dashboard-build
WORKDIR /dashboard
COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci
COPY dashboard/index.html dashboard/vite.config.js ./
COPY dashboard/src ./src
RUN npm run build

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 GATEWAY_STATIC_DIR=/app/dashboard_dist
WORKDIR /app
COPY pyproject.toml README.md requirements.lock ./
COPY gateway ./gateway
COPY migrations ./migrations
COPY prices.example.json ./prices.json
COPY prices.live.json ./prices.live.json
RUN pip install --no-cache-dir -c requirements.lock . && useradd --uid 10001 --create-home gateway
COPY --from=dashboard-build /dashboard/dist ./dashboard_dist
USER 10001
EXPOSE 8000
CMD ["python", "-m", "gateway.serve"]
