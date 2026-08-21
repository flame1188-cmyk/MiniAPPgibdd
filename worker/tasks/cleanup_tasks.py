"""
worker.tasks.cleanup_tasks — периодические задачи очистки (Sprint 7, Фаза C.3).

Очередь: celery (default, для beat-задач).

Задачи:
- cleanup_expired_caches — очистка протухших записей в PostgreSQL кэшах
  (cards_cache, clusters_cache, excel_cache, llm_cache).
  Запускается celery beat каждые 6 часов (crontab minute=0, hour="*/6").

- flush_stale_task_states — очистка устаревших task state в Redis
  (по updated_at, старше REDIS_TASK_STATE_TTL).
  Запускается celery beat nightly в 03:30 (crontab minute=30, hour=3).

Все async-db функции вызываются через asyncio.run() — Celery worker не имеет
event loop, но db.cards_cache.cleanup_old_cards и т.д. — async.

При недоступности БД — задача логирует WARNING и возвращает 0 (не падает).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

from worker.celery_app import app

logger = logging.getLogger(__name__)


# ============================================================
# Helper: запуск async-функции в sync-контексте Celery
# ============================================================
def _run_async(coro_factory):
    """Безопасно запускает async-функцию в Celery worker (без event loop).

    Args:
        coro_factory: callable, возвращающий coroutine (например,
                      lambda: cleanup_old_cards()).

    Returns:
        Результат async-функции, или 0 при ошибке.
    """
    try:
        return asyncio.run(coro_factory())
    except RuntimeError as exc:
        if "asyncio.run() cannot be called from a running event loop" in str(exc):
            logger.warning(
                "cleanup_tasks: вызвана из running event loop — "
                "используйте await напрямую"
            )
            return 0
        raise
    except Exception as exc:
        logger.warning(f"cleanup_tasks: async call failed: {exc}")
        return 0


# ============================================================
# cleanup_expired_caches
# ============================================================
@app.task(name="worker.tasks.cleanup_tasks.cleanup_expired_caches")
def cleanup_expired_caches() -> Dict[str, Any]:
    """Очищает протухшие записи в PostgreSQL кэшах.

    Вызывается celery beat каждые 6 часов.

    Удаляет:
    - cards_cache: записи старше 7 дней (TTL cards_cache)
    - clusters_cache: записи старше 7 дней
    - excel_cache: записи старше 7 дней
    - llm_cache: записи старше 24 часов (TTL llm_cache)

    Returns:
        dict:
        {
            "cards_deleted": int,
            "clusters_deleted": int,
            "excel_deleted": int,
            "llm_deleted": int,
            "total_deleted": int,
            "errors": list[str],   # список ошибок (пустой если все OK)
            "stub": False,         # маркер что это НЕ stub (для smoke-тестов)
        }
    """
    logger.info("cleanup_expired_caches: started")

    errors = []
    cards_deleted = 0
    clusters_deleted = 0
    llm_deleted = 0

    # cards_cache
    try:
        from miniapp.backend.db.cards_cache import cleanup_old_cards
        cards_deleted = _run_async(cleanup_old_cards)
        logger.info(f"cleanup_expired_caches: cards_deleted={cards_deleted}")
    except ImportError:
        errors.append("cards_cache module not importable")
    except Exception as exc:
        errors.append(f"cards_cache: {exc}")

    # clusters_cache
    try:
        from miniapp.backend.db.clusters_cache import cleanup_old_clusters
        clusters_deleted = _run_async(cleanup_old_clusters)
        logger.info(f"cleanup_expired_caches: clusters_deleted={clusters_deleted}")
    except ImportError:
        errors.append("clusters_cache module not importable")
    except Exception as exc:
        errors.append(f"clusters_cache: {exc}")

    # llm_cache
    try:
        from miniapp.backend.db.llm_cache import cleanup_expired_llm_cache
        llm_deleted = _run_async(cleanup_expired_llm_cache)
        logger.info(f"cleanup_expired_caches: llm_deleted={llm_deleted}")
    except ImportError:
        errors.append("llm_cache module not importable")
    except Exception as exc:
        errors.append(f"llm_cache: {exc}")

    total = cards_deleted + clusters_deleted + llm_deleted
    logger.info(
        f"cleanup_expired_caches: done — total={total} "
        f"(cards={cards_deleted}, clusters={clusters_deleted}, "
        f"llm={llm_deleted}), "
        f"errors={len(errors)}"
    )

    return {
        "cards_deleted": cards_deleted,
        "clusters_deleted": clusters_deleted,
        "llm_deleted": llm_deleted,
        "total_deleted": total,
        "errors": errors,
        "stub": False,
    }


# ============================================================
# flush_stale_task_states
# ============================================================
@app.task(name="worker.tasks.cleanup_tasks.flush_stale_task_states")
def flush_stale_task_states(max_age_seconds: int = 86400) -> Dict[str, Any]:
    """Очищает устаревшие task state записи в Redis.

    Вызывается celery beat в 03:30 nightly.

    Args:
        max_age_seconds: Удалять task state старше этого возраста (по updated_at).
                         По умолчанию 86400 (24 часа) — совпадает с TTL Redis.

    Returns:
        dict:
        {
            "task_states_deleted": int,
            "total_active": int,    # сколько осталось активных
            "stub": False,
        }
    """
    logger.info(
        f"flush_stale_task_states: started (max_age={max_age_seconds} сек)"
    )

    try:
        from worker.task_state import flush_stale, list_active_task_ids

        deleted = flush_stale(max_age_seconds=max_age_seconds)
        active = len(list_active_task_ids())

        logger.info(
            f"flush_stale_task_states: done — deleted={deleted}, "
            f"active_remaining={active}"
        )

        return {
            "task_states_deleted": deleted,
            "total_active": active,
            "stub": False,
        }

    except Exception as exc:
        logger.exception(f"flush_stale_task_states: failed — {exc}")
        return {
            "task_states_deleted": 0,
            "total_active": 0,
            "error": str(exc),
            "stub": False,
        }
