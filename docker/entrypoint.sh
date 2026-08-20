#!/bin/sh
# ============================================================
# entrypoint.sh — точка входа контейнера GIBDD Bot + Mini App
#
# Поддерживает 2 режима деплоя (управляется через DEPLOYMENT_MODE):
#
# 1. single (по умолчанию, backward compatible):
#    Запускает только `python main.py` — FastAPI + Telegram webhook.
#    Используется, если Celery не нужен или Redis недоступен.
#    Это текущий режим bothost (до Фазы C.3).
#
# 2. multi:
#    Запускает supervisord с 4 процессами:
#      - redis-server (брокер + result backend + pub/sub)
#      - uvicorn main:app (FastAPI + webhook)
#      - celery worker (4 очереди, concurrency=2 по умолчанию)
#      - celery beat (периодические задачи)
#    Используется после Фазы C.3, когда задачи переводятся на Celery.
#
# В обоих режимах:
#   - $PORT передаётся bothost (обычно 3000 или 8080)
#   - /app/data — персистентный volume (кэш камер, регионов, OSM)
#   - /data/redis — персистентный volume для Redis (если включён)
#
# Phase 0 Stabilization:
#   - Автоматический запуск Alembic migrations перед стартом приложения
#   - Задаёт default для CELERY_MAX_TASKS_PER_CHILD (=10) и
#     CELERY_WORKER_CONCURRENCY (=2) если они не заданы в env.
#     supervisord требует, чтобы переменные %(ENV_*)s существовали,
#     иначе падает с ошибкой подстановки.
# ============================================================
set -e

# Создаём директории для данных, если ещё нет
mkdir -p /app/data
mkdir -p /app/data/osm_cache /app/data/cameras
mkdir -p /app/data/files
mkdir -p /data/redis
mkdir -p /var/log/supervisor

# Phase 0: Автоматическое применение миграций Alembic
echo "[entrypoint] Applying database migrations..."
if command -v alembic >/dev/null 2>&1; then
    cd /app && alembic upgrade head || echo "[entrypoint] WARNING: Migration failed, continuing anyway..."
else
    echo "[entrypoint] WARNING: Alembic not installed, skipping migrations"
fi

# Stabilization A6: defaults для supervisord-подстановок.
# Если не задать — supervisord упадёт с "subject not defined" при парсинге
# %(ENV_CELERY_MAX_TASKS_PER_CHILD)s в supervisord.conf.
if [ -z "${CELERY_MAX_TASKS_PER_CHILD}" ]; then
    CELERY_MAX_TASKS_PER_CHILD=10
    export CELERY_MAX_TASKS_PER_CHILD
    echo "[entrypoint] CELERY_MAX_TASKS_PER_CHILD not set, defaulting to ${CELERY_MAX_TASKS_PER_CHILD}"
fi
if [ -z "${CELERY_WORKER_CONCURRENCY}" ]; then
    CELERY_WORKER_CONCURRENCY=2
    export CELERY_WORKER_CONCURRENCY
    echo "[entrypoint] CELERY_WORKER_CONCURRENCY not set, defaulting to ${CELERY_WORKER_CONCURRENCY} (optimized for 2GB RAM)"
fi
if [ -z "${PORT}" ]; then
    PORT=8080
    export PORT
fi

# Режим деплоя: single (по умолчанию) или multi
DEPLOYMENT_MODE="${DEPLOYMENT_MODE:-single}"

case "$DEPLOYMENT_MODE" in
    single)
        echo "[entrypoint] DEPLOYMENT_MODE=single — запуск python main.py"
        echo "[entrypoint] PORT=${PORT}, BOTHOST_DOMAIN=${BOTHOST_DOMAIN:-<не задан>}"
        exec python main.py
        ;;

    multi)
        echo "[entrypoint] DEPLOYMENT_MODE=multi — запуск supervisord"
        echo "[entrypoint] PORT=${PORT}, BOTHOST_DOMAIN=${BOTHOST_DOMAIN:-<не задан>}"
        echo "[entrypoint] Конфигурация:"
        echo "[entrypoint]   CELERY_WORKER_CONCURRENCY=${CELERY_WORKER_CONCURRENCY} (по умолчанию 2)"
        echo "[entrypoint]   CELERY_MAX_TASKS_PER_CHILD=${CELERY_MAX_TASKS_PER_CHILD} (по умолчанию 10)"
        echo "[entrypoint] Запускаемые процессы:"
        echo "[entrypoint]   1. redis-server (maxmemory 128mb, без AOF)"
        echo "[entrypoint]   2. uvicorn main:app --workers 1 --port ${PORT}"
        echo "[entrypoint]   3. celery worker --concurrency=${CELERY_WORKER_CONCURRENCY} -Q gibdd,llm,clusters,exports,celery"
        echo "[entrypoint]   4. celery beat --max-interval=60"
        echo "[entrypoint] Логи всех процессов в docker logs (stdout/stderr)"
        exec supervisord -n -c /etc/supervisord.conf
        ;;

    *)
        echo "[entrypoint] ERROR: неизвестный DEPLOYMENT_MODE='$DEPLOYMENT_MODE'" >&2
        echo "[entrypoint] Допустимые значения: single (по умолчанию), multi" >&2
        exit 1
        ;;
esac
