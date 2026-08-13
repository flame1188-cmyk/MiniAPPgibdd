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
#      - celery worker (4 очереди, concurrency=4)
#      - celery beat (периодические задачи)
#    Используется после Фазы C.3, когда задачи переводятся на Celery.
#
# В обоих режимах:
#   - $PORT передаётся bothost (обычно 3000 или 8080)
#   - /app/data — персистентный volume (кэш камер, регионов, OSM)
#   - /data/redis — персистентный volume для Redis (если включён)
# ============================================================
set -e

# Создаём директории для данных, если ещё нет
mkdir -p /app/data
mkdir -p /app/data/osm_cache /app/data/cameras
mkdir -p /data/redis
mkdir -p /var/log/supervisor

# Режим деплоя: single (по умолчанию) или multi
DEPLOYMENT_MODE="${DEPLOYMENT_MODE:-single}"

case "$DEPLOYMENT_MODE" in
    single)
        echo "[entrypoint] DEPLOYMENT_MODE=single — запуск python main.py"
        echo "[entrypoint] PORT=${PORT:-8080}, BOTHOST_DOMAIN=${BOTHOST_DOMAIN:-<не задан>}"
        exec python main.py
        ;;

    multi)
        echo "[entrypoint] DEPLOYMENT_MODE=multi — запуск supervisord"
        echo "[entrypoint] PORT=${PORT:-8080}, BOTHOST_DOMAIN=${BOTHOST_DOMAIN:-<не задан>}"
        echo "[entrypoint] Запускаемые процессы:"
        echo "[entrypoint]   1. redis-server (maxmemory 128mb, без AOF)"
        echo "[entrypoint]   2. uvicorn main:app --workers 1 --port ${PORT:-8080}"
        echo "[entrypoint]   3. celery worker --concurrency=4 -Q gibdd,llm,clusters,exports,celery"
        echo "[entrypoint]   4. celery beat --max-interval=60"
        echo "[entrypoint] Ожидаемое потребление RAM: ~700MB базовое, ~1.3GB пиковое"
        exec supervisord -n -c /etc/supervisord.conf
        ;;

    *)
        echo "[entrypoint] ERROR: неизвестный DEPLOYMENT_MODE='$DEPLOYMENT_MODE'" >&2
        echo "[entrypoint] Допустимые значения: single (по умолчанию), multi" >&2
        exit 1
        ;;
esac
