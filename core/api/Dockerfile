# One image for f42-api and f42-agent. Build context is the repo root:
#   docker build -f core/api/Dockerfile -t f42-web .
# APP_MODULE picks the service: core.api.app:app (f42-api, default) or
# core.api.agent_app:app (f42-agent).

FROM oven/bun:1.2 AS web
WORKDIR /src/app/frontend
COPY app/frontend/package.json app/frontend/bun.lock ./
COPY app/frontend/vendor ./vendor
RUN bun install --frozen-lockfile
COPY app/frontend/ ./
# vite writes to ../web/dist, that is /src/app/web/dist
RUN bun run build

FROM python:3.13-slim
WORKDIR /srv
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WEB_DIST=/srv/web/dist \
    APP_MODULE=core.api.app:app \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
COPY core/api/requirements.txt core/api/requirements.txt
RUN pip install --no-cache-dir -r core/api/requirements.txt
# Headless Chromium for the dossier PDF export (core/api/pdf.py), with the system libraries it needs.
RUN python -m playwright install --with-deps --only-shell chromium
COPY core/ core/
# L1's SocialCrawl client, which Ask calls through core/agent/tools/sc_adapter.py, reads the routes file two
# levels above core/collect, as the jobs image has it. core/config/caps.yaml comes with core/.
COPY docs/full-42/reference/sc_routes.json docs/full-42/reference/sc_routes.json
# The Ask agent's own packages, once lane L3 adds them.
RUN if [ -f core/agent/requirements.txt ]; then pip install --no-cache-dir -r core/agent/requirements.txt; fi
COPY --from=web /src/app/web/dist /srv/web/dist
RUN useradd --uid 10001 --no-create-home f42
USER f42
CMD ["sh", "-c", "exec uvicorn \"$APP_MODULE\" --host 0.0.0.0 --port \"${PORT:-8080}\" --workers 1"]
