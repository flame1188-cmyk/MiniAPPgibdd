"""
worker/tasks/llm_tasks.py — Celery задачи для LLM-аналитики (Sprint 7, Фаза C.3).

Очередь: llm (concurrency=2 в docker-compose).

Задачи:
- llm_summary_task — генерация LLM-резюме по подготовленным данным.
  Оборачивает core.run_llm_summary_sync.
  Прогресс пишется в task_state (status=running → done) и в pub/sub канал
  (для опционального SSE-стриминга).

- llm_qa_task — ответ на вопрос пользователя.
  Оборачивает core.ask_llm_question_sync.
  Результат публикуется в pub/sub канал "gibdd:llm:{task_id}" для SSE-стриминга
  в FastAPI-сторону (фронтенд видит токены по одному, а не ждёт весь ответ).

Подготовка данных (comparison, clusters_context, cross_tables_context) —
ответственность FastAPI-стороны (analytics_ops.ensure_comparison,
analytics_ops.format_clusters_for_prompt, ...). Celery получает ГОТОВЫЕ
контексты, не Task.

Backward compatibility:
  При USE_CELERY=false — dispatcher вызывает async-функции напрямую в
  FastAPI event loop (services/llm_ops.start_llm_summary / ask_llm_question).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from celery import Task as CeleryTask

from worker.celery_app import app
from worker.redis_pubsub import publish_done, publish_error, publish_progress
from worker.task_state import load_task_state, save_task_state

logger = logging.getLogger(__name__)


# ============================================================
# Хелпер: обновление llm_summary_state в snapshot
# ============================================================
def _update_llm_state_in_snapshot(
    task_id: str,
    *,
    status: Optional[str] = None,
    progress: Optional[int] = None,
    stage: Optional[str] = None,
    result: Optional[Any] = None,
    error: Optional[str] = None,
) -> None:
    """Обновляет llm_summary_state в snapshot'е задачи (в Redis)."""
    snapshot = load_task_state(task_id)
    if snapshot is None:
        # Snapshot не найден — задача могла быть создана в in-memory режиме
        return

    state = snapshot.get("llm_summary_state") or {}
    if status is not None:
        state["status"] = status
    if progress is not None:
        state["progress"] = progress
    if stage is not None:
        state["stage"] = stage
    if result is not None:
        state["result"] = result
    if error is not None:
        state["error"] = error
    if status == "running" and not state.get("started_at"):
        state["started_at"] = datetime.now(timezone.utc).isoformat()
    if status in ("done", "failed"):
        state["finished_at"] = datetime.now(timezone.utc).isoformat()

    snapshot["llm_summary_state"] = state
    snapshot["updated_at"] = datetime.now(timezone.utc).isoformat()

    # save_task_state ожидает объект с атрибутами
    class _TaskStub:
        pass
    stub = _TaskStub()
    for key, value in snapshot.items():
        setattr(stub, key, value)
    save_task_state(stub)


# ============================================================
# llm_summary_task
# ============================================================
@app.task(
    name="worker.tasks.llm_tasks.llm_summary_task",
    queue="llm",
    bind=True,
    base=CeleryTask,
    max_retries=1,
    acks_late=True,
)
def llm_summary_task(
    self: CeleryTask,
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
) -> Dict[str, Any]:
    """Генерирует LLM-резюме по подготовленным данным.

    Args:
        task_id: ID задачи (для обновления state в Redis).
        comparison: dict с метриками comparison (от analytics_ops.ensure_comparison).
        reg_name: Название региона (например "Республика Башкортостан").
        current_label: Метка текущего периода (например "2025 год").
        prev_label: Метка прошлого периода.
        clusters_context: Готовый clusters_context (пустая строка — без очагов).
        cross_tables_context: Готовый cross_tables_context.
        provider: "free" (ZhipuAI/GLM) или "paid" (DeepSeek).
        current_cards: Карточки текущего периода (только для paid-провайдера).
        prev_cards: Карточки прошлого периода (только для paid).
        publish_stream: True → публиковать прогресс в pub/sub канал
                        (для SSE-стриминга фронтенду).

    Returns:
        dict:
        {
            "ok": bool,
            "task_id": str,
            "text": str | None,        # LLM-резюме
            "provider": str,
            "generated_at": str,
            "error": str | None,
        }
    """
    log_prefix = f"Celery[llm_summary_task:{task_id}]"

    from miniapp.backend.core import run_llm_summary_sync

    logger.info(
        f"{log_prefix}: started — provider={provider}, "
        f"region={reg_name}, period={current_label}"
    )

    _update_llm_state_in_snapshot(
        task_id, status="running", progress=10, stage="Запрос к LLM..."
    )
    if publish_stream:
        publish_progress(task_id, 10, suffix="llm_summary")

    try:
        result = run_llm_summary_sync(
            comparison=comparison,
            reg_name=reg_name,
            current_label=current_label,
            prev_label=prev_label,
            clusters_context=clusters_context,
            cross_tables_context=cross_tables_context,
            provider=provider,
            current_cards=current_cards,
            prev_cards=prev_cards,
            log_prefix=log_prefix,
        )

        _update_llm_state_in_snapshot(
            task_id,
            status="done",
            progress=100,
            stage="Готово",
            result={
                "text": result["text"],
                "provider": result["provider"],
                "generated_at": result["generated_at"],
            },
        )
        if publish_stream:
            publish_done(
                task_id,
                full_text=result["text"],
                extra={
                    "provider": result["provider"],
                    "generated_at": result["generated_at"],
                },
                suffix="llm_summary",
            )

        logger.info(
            f"{log_prefix}: DONE — text={len(result['text'])} симв., "
            f"provider={result['provider']}"
        )
        return {
            "ok": True,
            "task_id": task_id,
            "text": result["text"],
            "provider": result["provider"],
            "generated_at": result["generated_at"],
            "error": None,
        }

    except Exception as exc:
        logger.exception(f"{log_prefix}: failed")
        _update_llm_state_in_snapshot(
            task_id, status="failed", progress=0, error=str(exc)
        )
        if publish_stream:
            publish_error(task_id, str(exc), suffix="llm_summary")
        return {
            "ok": False,
            "task_id": task_id,
            "text": None,
            "provider": provider,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "error": str(exc),
        }


# ============================================================
# llm_qa_task
# ============================================================
@app.task(
    name="worker.tasks.llm_tasks.llm_qa_task",
    queue="llm",
    bind=True,
    base=CeleryTask,
    max_retries=1,
    acks_late=True,
)
def llm_qa_task(
    self: CeleryTask,
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
) -> Dict[str, Any]:
    """Отвечает на вопрос пользователя по подготовленным данным.

    Args:
        task_id: ID задачи.
        question: Текст вопроса.
        comparison, reg_name, current_label, prev_label, qa_history,
        clusters_context, cross_tables_context, provider,
        current_cards, prev_cards: см. llm_summary_task.
        publish_stream: True (default) → публиковать ответ в pub/sub канал
                        gibdd:llm:{task_id}. Фронтенд стримит токены через SSE.

    Returns:
        dict:
        {
            "ok": bool,
            "task_id": str,
            "text": str | None,
            "provider": str,
            "generated_at": str,
            "error": str | None,
        }
    """
    log_prefix = f"Celery[llm_qa_task:{task_id}]"

    from miniapp.backend.core import ask_llm_question_sync

    logger.info(
        f"{log_prefix}: started — question={len(question)} симв., "
        f"provider={provider}, stream={publish_stream}"
    )

    if publish_stream:
        publish_progress(task_id, 10, suffix="llm")

    try:
        result = ask_llm_question_sync(
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
            log_prefix=log_prefix,
        )

        if result["ok"]:
            if publish_stream:
                # Публикуем полный ответ одним done-сообщением.
                # В Фазе C.4 можно разбить на токены, стримуя get_ai_answer_stream.
                publish_done(
                    task_id,
                    full_text=result["text"] or "",
                    extra={
                        "provider": result["provider"],
                        "generated_at": result["generated_at"],
                    },
                    suffix="llm",
                )
            logger.info(
                f"{log_prefix}: DONE — answer={len(result['text'] or '')} симв."
            )
        else:
            if publish_stream:
                publish_error(task_id, result.get("error", "Unknown"), suffix="llm")
            logger.warning(
                f"{log_prefix}: failed — {result.get('error')}"
            )

        return {
            "ok": result["ok"],
            "task_id": task_id,
            "text": result["text"],
            "provider": result["provider"],
            "generated_at": result["generated_at"],
            "error": result["error"],
        }

    except Exception as exc:
        logger.exception(f"{log_prefix}: exception")
        if publish_stream:
            publish_error(task_id, str(exc), suffix="llm")
        return {
            "ok": False,
            "task_id": task_id,
            "text": None,
            "provider": provider,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "error": str(exc),
        }
