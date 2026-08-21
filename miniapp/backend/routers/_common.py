"""
Общие хелперы и схемы для роутеров аналитики (clusters / point / llm).

Вынесено в отдельный модуль, чтобы избежать дублирования между
clusters.py и llm.py (которые оба используют AnalysisStatusResponse
и _state_to_response), а также чтобы все роутеры могли переиспользовать
_require_done_task без циклических импортов.

Зависимости (только services + telegram_auth — без других роутеров):
- services.gibdd_service: Task, TaskStatus, get_task_async
- telegram_auth: TelegramUser, get_current_user

Никаких зависимостей от clusters.py / point.py / llm.py — иначе будет цикл.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import HTTPException, status
from pydantic import BaseModel

from ..services.gibdd_service import Task, TaskStatus, get_task_async
from ..telegram_auth import TelegramUser, get_current_user  # noqa: F401 — re-export

logger = logging.getLogger(__name__)


# ============================================================
# Shared schemas
# ============================================================
class AnalysisStatusResponse(BaseModel):
    """
    Статус длительной операции (очаги / LLM-резюме).

    Используется в ClustersResponse и LLMSummaryResponse — поэтому живёт
    в _common, а не в одном из под-роутеров.
    """
    status: str  # idle | running | done | failed
    progress: int
    stage: str
    error: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


# ============================================================
# Shared helpers
# ============================================================
async def _require_done_task(task_id: str, user: TelegramUser) -> Task:
    """
    Проверяет, что задача принадлежит пользователю и завершена.
    Возвращает task или raises HTTPException.

    Используется всеми роутерами аналитики: clusters, point, llm.
    Вынесено в _common, чтобы не дублировать логику 3 раза.

    Hotfix (Sprint 7): логируем 404/403 на WARNING, чтобы в логах
    было видно, какой task_id и user_id запрашивает несуществующую
    задачу. Раньше бесконечный polling с 404 засорял access-log, но
    не оставлял понятного диагностического сообщения.
    """
    task = await get_task_async(task_id)
    if not task:
        logger.warning(
            f"_require_done_task: 404 task_id={task_id} user_id={user.id} "
            f"not found (ни in-memory, ни в БД). Возможно: контейнер "
            f"перезапущен, задача вытеснена из LRU и не сохранилась в БД, "
            f"или task_id никогда не существовал. Polling должен быть "
            f"остановлен клиентом."
        )
        raise HTTPException(status_code=404, detail="Task not found")
    if task.user_id != user.id:
        logger.warning(
            f"_require_done_task: 403 task_id={task_id} requester_user_id="
            f"{user.id} != owner_user_id={task.user_id} (access denied)"
        )
        raise HTTPException(status_code=403, detail="Access denied")
    if task.status != TaskStatus.DONE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Task status is '{task.status.value}', must be 'done' "
                f"to run analysis"
            ),
        )
    return task


def _state_to_response(state) -> AnalysisStatusResponse:
    """
    Преобразует AnalysisState в AnalysisStatusResponse.

    Используется роутерами clusters и llm (оба имеют long-running операции
    с state-машиной). Point — не использует (точка считается синхронно).
    """
    return AnalysisStatusResponse(
        status=state.status.value if hasattr(state.status, "value") else str(state.status),
        progress=state.progress,
        stage=state.stage,
        error=state.error,
        started_at=state.started_at.isoformat() if state.started_at else None,
        finished_at=state.finished_at.isoformat() if state.finished_at else None,
    )


# ============================================================
# Hotfix Sprint 7 (v2): "soft 404" для polling-эндпоинтов
# ============================================================
# Проблема: Telegram WebView кэширует старый JS-бандл очень агрессивно.
# Даже после деплоя нового бандла с фиксом (retry: false при 404,
# refetchInterval: false при 404), старый JS в кэше WebView продолжает
# бесконечный polling. No-cache middleware на index.html не помогает —
# WebView игнорирует Cache-Control.
#
# Решение: для polling-эндпоинтов (GET /clusters, GET /llm/summary)
# при несуществующей задаче возвращать НЕ 404, а 200 OK с
# status="failed" и error="Task not found". Это останавливает polling
# в ЛЮБОМ JS-коде (старом и новом), потому что оба проверяют
# status === 'failed' и возвращают refetchInterval: false.
#
# Frontend (новый) дополнительно проверяет error message и показывает
# понятный UI "Задача не найдена" (см. ClustersView.tsx).
#
# get_task_status_soft() возвращает (task, error_response):
# - (task, None) если задача найдена и принадлежит пользователю
# - (None, ClustersResponse/LLMSummaryResponse с status=failed) если 404/403
# - (None, None) если задача найдена, но статус != done (409 Conflict)
#   — в этом случае роутер должен сам решить, что вернуть
async def _check_task_soft(task_id: str, user: TelegramUser, error_label: str = "Задача не найдена"):
    """
    Soft-проверка задачи для polling-эндпоинтов.

    Возвращает кортеж (task, soft_error_response):
    - task — найденная задача (или None)
    - soft_error_response — AnalysisStatusResponse со status=failed
      (или None если задача найдена)

    Логирует WARNING при 404/403 (для диагностики).
    НЕ логирует при 409 (task not done) — это нормальный сценарий.
    """
    task = await get_task_async(task_id)
    if not task:
        logger.warning(
            f"_check_task_soft: 404 task_id={task_id} user_id={user.id} "
            f"not found (ни in-memory, ни в БД). Возвращаем soft-failed "
            f"ответ, чтобы остановить polling в старом JS."
        )
        soft_error = AnalysisStatusResponse(
            status="failed",
            progress=0,
            stage="",
            error=f"{error_label}: задача удалена из памяти сервера. "
                  f"Создайте новую выгрузку ДТП.",
        )
        return None, soft_error
    if task.user_id != user.id:
        logger.warning(
            f"_check_task_soft: 403 task_id={task_id} requester_user_id="
            f"{user.id} != owner_user_id={task.user_id} (access denied). "
            f"Возвращаем soft-failed ответ."
        )
        soft_error = AnalysisStatusResponse(
            status="failed",
            progress=0,
            stage="",
            error=f"Доступ запрещён: задача принадлежит другому пользователю.",
        )
        return None, soft_error
    return task, None
