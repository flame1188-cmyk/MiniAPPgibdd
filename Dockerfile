# ============================================================
# Multi-stage Dockerfile для GIBDD Bot + Mini App
#
# Стадии:
#   build-frontend  — сборка React/Vite
#   runtime-base    — общий Python runtime (без redis/supervisor)
#   runtime-multi    — multi-режим (supervisord + redis + celery)
#   runtime         — дефолтный таргет = single-режим (без redis/supervisor)
#
# Сборка:
#   docker build -t gibdd-bot-miniapp .                         # → single (дефолт)
#   docker build --target runtime-multi -t gibdd:multi .         # → multi
#
# На bothost.ru: дефолтный таргет (single) используется автоматически.
# Для multi-режима: DEPLOYMENT_MODE=multi + таргет runtime-multi.
# ============================================================

# --- Stage 1: Сборка frontend ---
FROM node:20-alpine AS build-frontend
WORKDIR /build

COPY miniapp/frontend/package.json miniapp/frontend/package-lock.json* ./
RUN npm ci --no-audit --no-fund || npm install --no-audit --no-fund

COPY miniapp/frontend/ ./

ARG APP_BUILD_VERSION
ARG APP_GIT_COMMIT
ARG APP_BUILD_TIME

ENV APP_BUILD_VERSION=${APP_BUILD_VERSION:-}
ENV APP_GIT_COMMIT=${APP_GIT_COMMIT:-}
ENV APP_BUILD_TIME=${APP_BUILD_TIME:-}
ENV VITE_APP_VERSION=${APP_BUILD_VERSION:-${APP_GIT_COMMIT:-}}

RUN if [ -z "$VITE_APP_VERSION" ] && command -v git >/dev/null 2>&1; then \
        GIT_VER="$(git rev-parse --short HEAD 2>/dev/null || true)"; \
        if [ -n "$GIT_VER" ]; then \
            export VITE_APP_VERSION="$GIT_VER" && export APP_BUILD_VERSION="$GIT_VER"; \
        fi; \
    fi; \
    if [ -z "$VITE_APP_VERSION" ]; then \
        export VITE_APP_VERSION="docker-$(date +%s)"; \
        export APP_BUILD_VERSION="$VITE_APP_VERSION"; \
    fi; \
    echo "[frontend build] VITE_APP_VERSION=$VITE_APP_VERSION"; \
    npm run build; \
    echo "$VITE_APP_VERSION" > /build/dist/build_version.txt; \
    echo "$VITE_APP_VERSION" > /build/dist/.build_version; \
    echo "${APP_BUILD_TIME:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}" > /build/dist/build_time.txt; \
    echo "${APP_BUILD_TIME:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}" > /build/dist/.build_time


# --- Stage 2: Base runtime (общий для single и multi) ---
# Базовые зависимости: Shapely (libgeos), lxml (libxml2), curl (healthcheck).
# Redis и supervisor НЕ устанавливаются здесь — они только в runtime-multi.
FROM python:3.11-slim AS runtime-base

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libgeos-dev \
    libxml2 \
    libxslt1.1 \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
COPY --from=build-frontend /build/dist ./miniapp/frontend/dist

RUN mkdir -p /app/data /app/data/osm_cache /app/data/cameras

ENV PYTHONPATH=/app
ENV CAMERA_DATA_DIR=/app/data
ENV PYTHONUNBUFFERED=1
ENV DEPLOYMENT_MODE=single

EXPOSE 3000 8080


# --- Stage 3: Multi-mode (supervisord + redis + celery) ---
FROM runtime-base AS runtime-multi

# Дополнительные системные зависимости только для multi-режима
RUN apt-get update && apt-get install -y --no-install-recommends \
    supervisor \
    redis-server \
    && rm -rf /var/lib/apt/lists/*

RUN test -x /usr/bin/redis-server \
    && /usr/bin/redis-server --version \
    || (echo "ERROR: /usr/bin/redis-server not found" && exit 1)

COPY docker/supervisord.conf /etc/supervisord.conf
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

RUN mkdir -p /data/redis /var/log/supervisor

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f "http://localhost:${PORT:-8080}/health/all" || exit 1

ENTRYPOINT ["/entrypoint.sh"]


# --- Stage 4 (дефолтный таргет): Single-mode ---
# Отнаследован от runtime-base без redis/supervisor.
# Это дефолтный таргет для bothost (docker build без --target).
FROM runtime-base AS runtime

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f "http://localhost:${PORT:-8080}/health/all" || exit 1

CMD ["python", "main.py"]
