# ============================================================
# Multi-stage Dockerfile для GIBDD Bot + Mini App
#
# Поддерживает 2 режима деплоя (через переменную DEPLOYMENT_MODE):
#
# 1. single (по умолчанию, backward compatible):
#    Запускает только `python main.py` — FastAPI + Telegram webhook.
#    Минимальное потребление RAM (~300-500MB).
#    Подходит, если Celery не нужен или Redis недоступен.
#
# 2. multi:
#    Запускает supervisord с 4 процессами:
#      - redis-server (брокер + result backend + pub/sub)
#      - uvicorn main:app (FastAPI + webhook)
#      - celery worker (4 очереди, concurrency=4)
#      - celery beat (периодические задачи)
#    Потребление RAM ~700MB-1.3GB (оптимизировано под 2GB bothost).
#
# Сборка:
#   docker build -t gibdd-bot-miniapp .
#
# Запуск (локально, single-режим):
#   docker run -d --env-file .env -p 8080:8080 gibdd-bot-miniapp
#
# Запуск (multi-режим):
#   docker run -d --env-file .env -e DEPLOYMENT_MODE=multi -p 8080:8080 gibdd-bot-miniapp
#
# На bothost.ru: просто укажите этот Dockerfile как источник,
# bothost автоматически соберёт и запустит.
# Для multi-режима задайте DEPLOYMENT_MODE=multi в переменных окружения bothost.
# ============================================================

# --- Stage 1: Сборка frontend ---
FROM node:20-alpine AS build-frontend
WORKDIR /build

# Кэшируем установку зависимостей
COPY miniapp/frontend/package.json miniapp/frontend/package-lock.json* ./
RUN npm ci --no-audit --no-fund || npm install --no-audit --no-fund

# Копируем исходники и собираем
COPY miniapp/frontend/ ./
RUN npm run build


# --- Stage 2: Runtime ---
FROM python:3.11-slim AS runtime

# Системные зависимости для Shapely + httpx + supervisor + redis
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libgeos-dev \
    libxml2 \
    libxslt1.1 \
    curl \
    supervisor \
    redis-server \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Устанавливаем Python-зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь код проекта (gibdd-bot + miniapp + worker/)
COPY . .

# Копируем собранный frontend
COPY --from=build-frontend /build/dist ./miniapp/frontend/dist

# Копируем конфигурацию supervisor и entrypoint
COPY docker/supervisord.conf /etc/supervisord.conf
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Создаём директории для данных
# /app/data — persistent volume на Bothost (переживает redeploy).
#   Внутри: osm_cache/ (предкэш границ НП), cameras/ (кэш камер).
# /data/redis — persistent volume для Redis (RDB snapshots, если включены).
# /var/log/supervisor — логи supervisor и каждой программы отдельно.
RUN mkdir -p /app/data /app/data/osm_cache /app/data/cameras \
    /data/redis /var/log/supervisor

# Переменные окружения по умолчанию.
# PORT не задаём здесь жёстко — bothost передаёт свой PORT через env (обычно 3000).
# Если запускаем локально без bothost — main.py использует 8080 по умолчанию.
ENV PYTHONPATH=/app
ENV CAMERA_DATA_DIR=/app/data
ENV PYTHONUNBUFFERED=1
# Режим деплоя: single (по умолчанию, только API) или multi (supervisord с 4 процессами)
ENV DEPLOYMENT_MODE=single

# Healthcheck: берём порт из $PORT, чтобы он работал и на bothost (3000), и локально (8080).
# В multi-режиме supervisord запускает Redis/api/worker/beat, но /health проверяет только API.
# Дополнительные проверки: /health/redis, /health/celery (см. main.py Sprint 7).
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f "http://localhost:${PORT:-8080}/health" || exit 1

# Открываем оба порта: 3000 (bothost) и 8080 (локальный дефолт main.py).
# EXPOSE — это метаданные, bothost всё равно использует поле «Порт» из дашборда.
EXPOSE 3000 8080

# Точка входа: переключает между single (python main.py) и multi (supervisord)
# на основе DEPLOYMENT_MODE env var.
ENTRYPOINT ["/entrypoint.sh"]
