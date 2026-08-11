"""
worker/celery_app.py — конфигурация Celery для Sprint 7 (вариант C).

4 очереди:
- gibdd     — выгрузка карточек ДТП (long: 30-60 сек), concurrency=3
- llm       — LLM резюме + Q&A (long: 30-60 сек), concurrency=2
- clusters  — расчёт очагов (CPU-bound: 15-30 сек), concurrency=2
- exports   — генерация Excel/HTML (medium: 5-10 сек), concurrency=4
- celery    — default очередь (для служебных задач и beat)

Routing:
- Каждая задача явно привязана к очереди через @task(queue="...")
- Beat-задачи идут в default очередь "celery"

Schedule (celery beat):
- cleanup_expired_caches — каждые 6 часов (cards/clusters/excel/llm_cache)
- flush_stale_task_states — каждые 24 часа (Redis task state TTL)

Зависимости:
- config.py — читает REDIS_URL, CELERY_BROKER_URL, CELERY_RESULT_BACKEND, USE_CELERY
- worker.tasks.* — импортируются для регистрации в Celery app
"""
from __future__ import annotations

import logging
from pathlib import Path

from celery import Celery
from celery.schedules import crontab

import config

logger = logging.getLogger(__name__)

# Корень проекта (для sys.path в worker-процессе)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
import sys
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ============================================================
# Создание Celery app
# ============================================================
app = Celery(
    "gibdd_worker",
    broker=config.CELERY_BROKER_URL,
    backend=config.CELERY_RESULT_BACKEND,
    include=[
        # Импортируем модули с задачами, чтобы Celery их зарегистрировал
        # (раскомментируем по мере реализации в Фазе C.3)
        # "worker.tasks.gibdd_tasks",
        # "worker.tasks.llm_tasks",
        # "worker.tasks.clusters_tasks",
        # "worker.tasks.exports_tasks",
        # "worker.tasks.cleanup_tasks",
    ],
)


# ============================================================
# Конфигурация
# ============================================================
app.conf.update(
    # --- Serializer ---
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Europe/Moscow",
    enable_utc=True,

    # --- Limits ---
    task_soft_time_limit=config.CELERY_TASK_SOFT_TIME_LIMIT,  # 540 сек
    task_time_limit=config.CELERY_TASK_TIME_LIMIT,            # 600 сек
    worker_max_tasks_per_child=config.CELERY_MAX_TASKS_PER_CHILD,  # 50

    # --- Reliability ---
    task_acks_late=True,           # ack после выполнения, а не до — упавший worker не теряет задачу
    task_reject_on_worker_lost=True,  # при падении worker — задача возвращается в очередь
    worker_prefetch_multiplier=1,  # не брать больше 1 задачи на worker-поток (чтобы long-задачи не блокировали)

    # --- Result backend ---
    result_expires=3600,           # результаты хранятся 1 час (дольше не нужно — состояние в Redis)
    result_persistent=False,       # Redis и так персистентный через appendonly

    # --- Queues ---
    # Каждая очередь имеет свой лимит concurrency через запуск отдельных worker:
    #   celery -A worker.celery_app worker -Q gibdd --concurrency=3
    #   celery -A worker.celery_app worker -Q llm --concurrency=2
    #   celery -A worker.celery_app worker -Q clusters --concurrency=2
    #   celery -A worker.celery_app worker -Q exports --concurrency=4
    # Или один worker на все очереди (по умолчанию в docker-compose):
    #   celery -A worker.celery_app worker -Q gibdd,llm,clusters,exports,celery --concurrency=4
    task_queues={
        "gibdd": {"exchange": "gibdd", "routing_key": "gibdd"},
        "llm": {"exchange": "llm", "routing_key": "llm"},
        "clusters": {"exchange": "clusters", "routing_key": "clusters"},
        "exports": {"exchange": "exports", "routing_key": "exports"},
        "celery": {"exchange": "celery", "routing_key": "celery"},
    },
    task_default_queue="celery",
    task_default_exchange="celery",
    task_default_routing_key="celery",

    # --- Beat (периодические задачи) ---
    beat_schedule={
        # Cleanup протухших кэшей в PostgreSQL
        "cleanup-expired-caches": {
            "task": "worker.tasks.cleanup_tasks.cleanup_expired_caches",
            "schedule": crontab(minute=0, hour="*/6"),  # каждые 6 часов
        },
        # Cleanup устаревших task state в Redis
        "flush-stale-task-states": {
            "task": "worker.tasks.cleanup_tasks.flush_stale_task_states",
            "schedule": crontab(minute=30, hour=3),  # в 03:30 nightly
        },
    },

    # --- Monitoring ---
    worker_send_task_events=True,
    task_send_sent_event=True,
)


# ============================================================
# Health-check helper (используется в /health/celery)
# ============================================================
def ping_worker(timeout: float = 1.0) -> dict:
    """Пингует все worker-инстансы через celery inspect.

    Возвращает dict:
    {
        "ok": bool,
        "workers": ["celery@worker-1", "celery@worker-2"],
        "ping_count": int,
        "error": str | None
    }
    """
    if not config.REDIS_URL:
        return {"ok": False, "workers": [], "ping_count": 0, "error": "REDIS_URL not configured"}

    try:
        inspect = app.control.inspect(timeout=timeout)
        ping_results = inspect.ping() or {}
        return {
            "ok": len(ping_results) > 0,
            "workers": list(ping_results.keys()),
            "ping_count": len(ping_results),
            "error": None if ping_results else "no workers responded",
        }
    except Exception as exc:
        return {"ok": False, "workers": [], "ping_count": 0, "error": str(exc)}
