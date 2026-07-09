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
# 'pg' bundles the psycopg driver; 'rag' bundles Chroma + document parsers;
# 'browser' installs the Playwright Python API. Browser binaries remain isolated
# in the optional browser-runtime target below.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv pip install --python "$VIRTUAL_ENV/bin/python" ".[serve,pg,rag,browser]"

# ---- runtime: minimal image, non-root ----
FROM python:3.12.10-slim AS runtime

# ========== 新增：替换 APT 源为阿里云镜像（加速系统依赖下载） ==========
# Debian 12 (Bookworm) 的 sources.list 文件位置（兼容新旧格式）
RUN if [ -f /etc/apt/sources.list ]; then \
        sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list && \
        sed -i 's/security.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list; \
    fi && \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list.d/debian.sources && \
        sed -i 's/security.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list.d/debian.sources; \
    fi
# ==================================================================

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    # The package is pip-installed into the venv, so it can't derive the config
    # root from __file__. Pin it to /app where tenants/agents/contexts/skills are copied
    # and the data volume is mounted.
    AGENTKIT_ROOT=/app

# OCR support for scanned PDFs and embedded Word images. chi-sim lets the
# default eng+chi_sim OCR language setting work for Chinese enterprise docs.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-chi-sim \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 appuser

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY src ./src
COPY agents ./agents
COPY contexts ./contexts
COPY skills ./skills
COPY tenants ./tenants

# Runtime scratch/data path. In Docker compose, durable runtime storage is
# PostgreSQL; this path remains writable for local files and compatibility.
RUN mkdir -p /app/data && chown -R appuser:appuser /app

USER appuser

EXPOSE 8501

# Liveness probe against the public, auth-free health endpoint.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8501/healthz', timeout=3).status==200 else 1)"]

CMD ["gunicorn", "--bind", "0.0.0.0:8501", "--workers", "2", "--timeout", "120", "agentkit.web.app:app"]

# Optional browser-enabled runtime. Build explicitly with
# `--target browser-runtime`; the normal final target stays small and does not
# download Chromium. Authentication state should be supplied through a mounted
# storage-state directory, never baked into the image.
FROM runtime AS browser-runtime

USER root

# ========== 新增：设置 Playwright 下载镜像（加速 Chromium 下载） ==========
ENV PLAYWRIGHT_DOWNLOAD_HOST=https://registry.npmmirror.com/-/binary/playwright
# ======================================================================
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN python -m playwright install --with-deps chromium \
    && chown -R appuser:appuser /ms-playwright
USER appuser

# Keep the default docker build on the minimal runtime target.
FROM runtime AS final