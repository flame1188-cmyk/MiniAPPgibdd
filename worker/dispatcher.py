"""
worker/dispatcher.py — dual-mode диспетчер (Sprint 7, Фаза C.3).

Назначение:
  Единая точка входа для запуска долгих операций (pipeline / LLM / clusters /
  exports). Диспетчер решает, куда отправить задачу:

  - USE_CELERY=true И REDIS_URL задан → отправить в Celery очередь.
    FastAPI сразу возвращает task_id, фронтенд polling'ом читает статус
    из Redis (worker.task_state).
  - Иначе → запустить async-функцию в текущем FastAPI event loop через
    asyncio.create_task (legacy path, Sprint 6 behaviour).

  Это даёт:
  1. Zero-downtime миграцию — переключение фича-флагом, без изменения
     router-кода.
  2. Dev/тесты без Redis — все работает in-memory.
  3. Production с Redis — масштабирование через отдельных Celery worker'ов.

API:
  - dispatch_execute_pipeline(task_id, ...) → str
    Возвращает task_id (Celery async result id или просто task_id для in-memory).
  - dispatch_llm_summary(task_id, ...) → str
  - dispatch_llm_qa(task_id, ...) → str
  - dispatch_clusters_calc(task_id, ...) → str
  - dispatch_generate_excel(...) → str
  - dispatch_generate_map(...) → str

  Все функции СИНХРОННЫЕ — вызываются из async-кода FastAPI через
  await asyncio.to_thread(dispatch_*, ...) ИЛИ напрямую (если в sync-контексте).
  Celery .delay() — синхронный (отправляет в broker и возвращается).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ============================================================
# Config helpers
# ============================================================
def _is_celery_enabled() -> bool:
    """True если USE_CELERY=true И REDIS_URL задан."""
    try:
        import config
        return bool(getattr(config, "USE_CELERY", False)) and bool(
            getattr(config, "REDIS_URL", "")
        )
    except Exception:
        return False


def _backend_name() -> str:
    """Возвращает 'celery' или 'in_memory' — для логирования."""
    return "celery" if _is_celery_enabled() else "in_memory"


# ============================================================
# Хелпер для in-memory path (asyncio.create_task в текущем event loop)
# ============================================================
def _schedule_async(coro) -> str:
    """Планирует coroutine в текущем event loop (fire-and-forget).

    Возвращает пустую строку — task_id уже известен вызывающему коду
    (он его передал в dispatch_*).
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # Нет running event loop — вызывается из sync-контекста.
        # Запускаем в новом loop (это может быть тест или Celery worker).
        logger.warning(
            "dispatcher: no running event loop — running coroutine in new loop"
        )
        asyncio.run(coro)
        return ""

    task = loop.create_task(coro)

    # done-callback — логируем ошибки, чтобы не терять их молча
    def _on_done(fut: asyncio.Future):
        try:
            fut.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning(f"dispatcher: in-memory task failed: {exc}")

    task.add_done_callback(_on_done)
    return ""


# ============================================================
# 1. execute_pipeline
# ============================================================
def dispatch_execute_pipeline(
    task_id: str,
    dat_list: List[str],
    reg_code: str,
    region_name: str,
    period_label: str,
    prev_dat_list: Optional[List[str]] = None,
    prev_label: Optional[str] = None,
    user_id: int = 0,
    raw_query: str = "",
) -> str:
    """Запускает pipeline выгрузки ДТП.

    Returns:
        task_id (тот же, что передан). В Celery-режиме Celery async result id
        НЕ возвращается — task_id является ключом для polling статуса.

    Side effects:
        - В Celery-режиме: отправляет execute_pipeline_task в очередь "gibdd".
        - В in-memory: запускает services.pipeline.execute_task(task_id)
          в текущем event loop (fire-and-forget).
    """
    if _is_celery_enabled():
        from worker.tasks.gibdd_tasks import execute_pipeline_task

        logger.info(
            f"dispatcher[celery]: dispatch execute_pipeline task_id={task_id} "
            f"region={reg_code} period={period_label}"
        )
        # .delay() — синхронный: отправляет в broker и возвращается
        execute_pipeline_task.delay(
            task_id=task_id,
            dat_list=dat_list,
            reg_code=reg_code,
            region_name=region_name,
            period_label=period_label,
            prev_dat_list=prev_dat_list,
            prev_label=prev_label,
            user_id=user_id,
            raw_query=raw_query,
        )
        return task_id

    # in-memory path
    logger.info(
        f"dispatcher[in_memory]: dispatch execute_pipeline task_id={task_id} "
        f"region={reg_code} period={period_label}"
    )
    from miniapp.backend.services.pipeline import execute_task
    _schedule_async(execute_task(task_id))
    return task_id


# ============================================================
# 2. llm_summary
# ============================================================
def dispatch_llm_summary(
    task_id: str,
    comparison: Dict[str, Any],
    reg_name: str,
    current_label: str,
    prev_label: str,
    clusters_context: str = "",
    cross_tables_context: str = "",
    provider: str = "free",
    current_cards: Optional[List[Dict[str, Any]]] = None,
    prev_cards: Optional[List[Dict[str, Any]]] = None,
    publish_stream: bool = False,
) -> str:
    """Запускает LLM-резюме.

    Returns:
        task_id.
    """
    if _is_celery_enabled():
        from worker.tasks.llm_tasks import llm_summary_task

        logger.info(
            f"dispatcher[celery]: dispatch llm_summary task_id={task_id} "
            f"provider={provider}"
        )
        llm_summary_task.delay(
            task_id=task_id,
            comparison=comparison,
            reg_name=reg_name,
            current_label=current_label,
            prev_label=prev_label,
            clusters_context=clusters_context,
            cross_tables_context=cross_tables_context,
            provider=provider,
            current_cards=current_cards,
            prev_cards=prev_cards,
            publish_stream=publish_stream,
        )
        return task_id

    # in-memory: вызываем async-функцию напрямую
    logger.info(
        f"dispatcher[in_memory]: dispatch llm_summary task_id={task_id} "
        f"provider={provider}"
    )
    from miniapp.backend.services.llm_ops import start_llm_summary
    # Получаем Task объект — он нужен start_llm_summary
    # (start_llm_summary принимает Task, не task_id)
    from miniapp.backend.services.task_registry import get_task_async

    async def _run():
        task = await get_task_async(task_id)
        if task is None:
            logger.error(
                f"dispatcher: task {task_id} not found for llm_summary"
            )
            return
        await start_llm_summary(task, provider=provider)

    _schedule_async(_run())
    return task_id


# ============================================================
# 3. llm_qa
# ============================================================
def dispatch_llm_qa(
    task_id: str,
    question: str,
    comparison: Dict[str, Any],
    reg_name: str,
    current_label: str,
    prev_label: str,
    qa_history: Optional[List[Dict[str, str]]] = None,
    clusters_context: str = "",
    cross_tables_context: str = "",
    provider: str = "free",
    current_cards: Optional[List[Dict[str, Any]]] = None,
    prev_cards: Optional[List[Dict[str, Any]]] = None,
    publish_stream: bool = True,
) -> str:
    """Запускает LLM Q&A.

    Returns:
        task_id.
    """
    if _is_celery_enabled():
        from worker.tasks.llm_tasks import llm_qa_task

        logger.info(
            f"dispatcher[celery]: dispatch llm_qa task_id={task_id} "
            f"question_len={len(question)} provider={provider}"
        )
        llm_qa_task.delay(
            task_id=task_id,
            question=question,
            comparison=comparison,
            reg_name=reg_name,
            current_label=current_label,
            prev_label=prev_label,
            qa_history=qa_history,
            clusters_context=clusters_context,
            cross_tables_context=cross_tables_context,
            provider=provider,
            current_cards=current_cards,
            prev_cards=prev_cards,
            publish_stream=publish_stream,
        )
        return task_id

    # in-memory
    logger.info(
        f"dispatcher[in_memory]: dispatch llm_qa task_id={task_id} "
        f"question_len={len(question)}"
    )
    from miniapp.backend.services.llm_ops import ask_llm_question
    from miniapp.backend.services.task_registry import get_task_async

    async def _run():
        task = await get_task_async(task_id)
        if task is None:
            logger.error(f"dispatcher: task {task_id} not found for llm_qa")
            return
        await ask_llm_question(
            task=task,
            question=question,
            provider=provider,
        )

    _schedule_async(_run())
    return task_id


# ============================================================
# 4. clusters_calc
# ============================================================
def dispatch_clusters_calc(
    task_id: str,
    cards: List[Dict[str, Any]],
    prev_cards: Optional[List[Dict[str, Any]]] = None,
    prev_label: Optional[str] = None,
    reg_code: Optional[str] = None,
    region_name: str = "",
    current_label: str = "",
    dat_list: Optional[List[str]] = None,
    prev_dat_list: Optional[List[str]] = None,
) -> str:
    """Запускает расчёт очагов концентрации.

    Returns:
        task_id.
    """
    if _is_celery_enabled():
        from worker.tasks.clusters_tasks import clusters_calc_task

        logger.info(
            f"dispatcher[celery]: dispatch clusters_calc task_id={task_id} "
            f"region={reg_code} cards={len(cards)}"
        )
        clusters_calc_task.delay(
            task_id=task_id,
            cards=cards,
            prev_cards=prev_cards,
            prev_label=prev_label,
            reg_code=reg_code,
            region_name=region_name,
            current_label=current_label,
            dat_list=dat_list,
            prev_dat_list=prev_dat_list,
        )
        return task_id

    # in-memory
    logger.info(
        f"dispatcher[in_memory]: dispatch clusters_calc task_id={task_id} "
        f"region={reg_code} cards={len(cards)}"
    )
    from miniapp.backend.services.clusters_ops import start_clusters_calculation
    from miniapp.backend.services.task_registry import get_task_async

    async def _run():
        task = await get_task_async(task_id)
        if task is None:
            logger.error(
                f"dispatcher: task {task_id} not found for clusters_calc"
            )
            return
        await start_clusters_calculation(task)

    _schedule_async(_run())
    return task_id


# ============================================================
# 5. generate_excel
# ============================================================
def dispatch_generate_excel(
    file1_data: List[Dict[str, Any]],
    file2_data: List[Dict[str, Any]],
    reg_code: str = "",
    dat_list: Optional[List[str]] = None,
    region_name: str = "",
    period_label: str = "",
    total_dtp: int = 0,
    total_dead: int = 0,
    total_injured: int = 0,
    use_cache: bool = True,
) -> str:
    """Запускает генерацию Excel-байтов.

    Returns:
        В Celery-режиме — Celery async result id (для отслеживания).
        В in-memory — пустая строка (результат остаётся в task).
    """
    if _is_celery_enabled():
        from worker.tasks.exports_tasks import generate_excel_task

        logger.info(
            f"dispatcher[celery]: dispatch generate_excel region={reg_code} "
            f"file1_rows={len(file1_data)}"
        )
        result = generate_excel_task.delay(
            file1_data=file1_data,
            file2_data=file2_data,
            reg_code=reg_code,
            dat_list=dat_list,
            region_name=region_name,
            period_label=period_label,
            total_dtp=total_dtp,
            total_dead=total_dead,
            total_injured=total_injured,
            use_cache=use_cache,
        )
        return result.id

    # in-memory — возвращает пустую строку, результат остаётся в вызывающем коде
    logger.info(
        f"dispatcher[in_memory]: dispatch generate_excel region={reg_code} "
        f"file1_rows={len(file1_data)}"
    )
    from miniapp.backend.core import generate_excel_bytes_sync
    # В in-memory режиме вызывающий код должен вызвать generate_excel_bytes_sync
    # напрямую (это sync CPU-bound, выносится в asyncio.to_thread)
    raise NotImplementedError(
        "generate_excel in in-memory mode: use miniapp.backend.core."
        "generate_excel_bytes_sync directly via asyncio.to_thread"
    )


# ============================================================
# 6. generate_map
# ============================================================
def dispatch_generate_map(
    cards: List[Dict[str, Any]],
    region_name: str,
    period_label: str,
    cameras: Optional[List[Dict[str, Any]]] = None,
    prev_cards: Optional[List[Dict[str, Any]]] = None,
    prev_label: Optional[str] = None,
) -> str:
    """Запускает генерацию HTML-карты.

    Returns:
        В Celery-режиме — Celery async result id.
        В in-memory — NotImplementedError (используйте core напрямую).
    """
    if _is_celery_enabled():
        from worker.tasks.exports_tasks import generate_map_task

        logger.info(
            f"dispatcher[celery]: dispatch generate_map region={region_name} "
            f"cards={len(cards)}"
        )
        result = generate_map_task.delay(
            cards=cards,
            region_name=region_name,
            period_label=period_label,
            cameras=cameras,
            prev_cards=prev_cards,
            prev_label=prev_label,
        )
        return result.id

    raise NotImplementedError(
        "generate_map in in-memory mode: use miniapp.backend.core."
        "generate_map_html_sync directly via asyncio.to_thread"
    )


# ============================================================
# Health-check
# ============================================================
def healthcheck() -> Dict[str, Any]:
    """Возвращает статус диспетчера для /health/celery."""
    return {
        "backend": _backend_name(),
        "celery_enabled": _is_celery_enabled(),
    }
