# Dockerfile — the Flask REST adapter only (the MCP stdio transport runs locally, not here).
#
# Builds a slim, hash-verified image from requirements.prod.txt (REST runtime deps only — no
# `mcp`, no dev/test tooling), runs as a non-root user, and serves api:create_app() via gunicorn.
#
# Secrets are NEVER baked in: REST_API_KEY and the Klaviyo per-account keys come from the
# environment at run time, and accounts.toml is mounted (see docker-compose.yml).
#
#   docker build -t klaviyo-mcp-rest .
#   docker run --rm -p 8080:8080 --env-file .env -e ACCOUNTS_FILE=/app/accounts.toml \
#       -v "$PWD/accounts.toml:/app/accounts.toml:ro" klaviyo-mcp-rest

FROM python:3.11-slim AS runtime

# Fail fast, no .pyc clutter, unbuffered logs (structlog already writes to stderr).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    REST_HOST=0.0.0.0 \
    REST_PORT=8080

WORKDIR /app

# Install runtime deps first (cached layer), hash-verified exactly like the documented install.
COPY requirements.prod.txt ./
RUN python -m pip install --upgrade "pip==24.2" \
    && pip install --require-hashes -r requirements.prod.txt

# Only the code the REST adapter needs (api + business layer). server.py and tests are excluded.
COPY api/ ./api/
COPY klaviyo_analytics/ ./klaviyo_analytics/

# Run as an unprivileged user.
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8080

# Liveness: /health answers 200 without auth.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health').status==200 else 1)"

# Two workers is a sane default for a low-traffic reporting API; override with GUNICORN_CMD_ARGS.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "api:create_app()"]
