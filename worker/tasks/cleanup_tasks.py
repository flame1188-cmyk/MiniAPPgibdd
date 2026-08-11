"""
worker.tasks.cleanup_tasks — периодические задачи очистки (Sprint 7, Фаза C.3).

Очередь: celery (default, для beat-задач).

Задачи:
- cleanup_expired_caches — очистка протухших записей в PostgreSQL кэшах
  (cards_cache, clusters_cache, excel_cache, llm_cache)
- flush_stale_task_states — очистка устаревших task state в Redis

Сейчас — заглушки. Полная реализация в Фазе C.3.
"""
from __future__ import annotations

import logging

from worker.celery_app import app

logger = logging.getLogger(__name__)


@app.task(name="worker.tasks.cleanup_tasks.cleanup_expired_caches")
def cleanup_expired_caches() -> dict:
    """Очищает протухшие записи в PostgreSQL кэшах.

    Вызывается celery beat каждые 6 часов.

    Returns:
        dict с количеством удалённых записей по каждой таблице.
    """
    logger.info("Sprint 7 stub: cleanup_expired_caches called (will be implemented in Phase C.3)")
    # TODO Фаза C.3: реализовать через db.cards_cache.cleanup_old_cards(),
    # db.clusters_cache.cleanup_old_clusters(), db.excel_cache.cleanup_old_excel(),
    # db.llm_cache.cleanup_expired_llm_cache()
    return {
        "cards_deleted": 0,
        "clusters_deleted": 0,
        "excel_deleted": 0,
        "llm_deleted": 0,
        "stub": True,
    }


@app.task(name="worker.tasks.cleanup_tasks.flush_stale_task_states")
def flush_stale_task_states() -> dict:
    """Очищает устаревшие task state записи в Redis.

    Вызывается celery beat в 03:30 nightly.

    Returns:
        dict с количеством удалённых task state записей.
    """
    logger.info("Sprint 7 stub: flush_stale_task_states called (will be implemented in Phase C.3)")
    # TODO Фаза C.3: реализовать через worker.task_state.flush_stale()
    return {
        "task_states_deleted": 0,
        "stub": True,
    }
