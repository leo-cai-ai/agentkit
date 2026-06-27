# syntax=docker/dockerfile:1

# ---- builder: install dependencies into a venv with uv ----
FROM python:3.12.10-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_NO_CACHE=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

RUN pip install --no-cache-dir uv && python -m venv "$VIRTUAL_ENV"

WORKDIR /app

# Install dependencies first (better layer caching), then the package itself.
# 'pg' bundles the psycopg driver so the PostgreSQL/pgvector backend works
# out of the box (activate via AGENTKIT_VECTOR_STORE_BACKEND=postgres).
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv pip install --python "$VIRTUAL_ENV/bin/python" ".[serve,pg]"

# ---- runtime: minimal image, non-root ----
FROM python:3.12.10-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    # The package is pip-installed into the venv, so it can't derive the config
    # root from __file__. Pin it to /app where tenants/prompts/skills are copied
    # and the data volume is mounted.
    AGENTKIT_ROOT=/app

# Create an unprivileged user to run the app.
RUN useradd --create-home --uid 10001 appuser

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY src ./src
COPY prompts ./prompts
COPY skills ./skills
COPY tenants ./tenants

# Runtime data (SQLite audit DBs) is written here; mount a volume over it.
RUN mkdir -p /app/data && chown -R appuser:appuser /app

USER appuser

EXPOSE 8501

# Liveness probe against the public, auth-free health endpoint.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8501/healthz', timeout=3).status==200 else 1)"]

CMD ["gunicorn", "--bind", "0.0.0.0:8501", "--workers", "2", "--timeout", "120", "agentkit.web.app:app"]
