"""
Роутер LLM-аналитики: providers + summary + Q&A.

Endpoints:
- GET  /api/dtp/tasks/{task_id}/llm/providers  — статус LLM-провайдеров
- POST /api/dtp/tasks/{task_id}/llm/summary    — запуск генерации резюме (async)
- GET  /api/dtp/tasks/{task_id}/llm/summary    — статус/результат резюме
- POST /api/dtp/tasks/{task_id}/llm/ask        — вопрос нейросети (sync)
- GET  /api/dtp/tasks/{task_id}/llm/qa-history — история вопросов/ответов

Все endpoints требуют готовую задачу (task.status == 'done').

Вынесено из routers/analyze.py (Sprint 3).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..services.gibdd_service import (
    AnalysisStatus,
    ask_llm_question,
    get_llm_providers_status,
    start_llm_summary,
)
from ..telegram_auth import TelegramUser, get_current_user
from ._common import AnalysisStatusResponse, _require_done_task, _state_to_response

logger = logging.getLogger(__name__)

# Без prefix — analyze.py (facade) задаёт /dtp на агрегированном router.
router = APIRouter(tags=["analyze"])


# ============================================================
# Schemas
# ============================================================
class LLMProvidersResponse(BaseModel):
    free: bool
    paid: bool
    free_model: str
    paid_model: str


class LLMSummaryRequest(BaseModel):
    provider: str = Field(
        default="free",
        description="'free' (ZhipuAI/GLM) или 'paid' (DeepSeek)",
    )


class LLMSummaryResult(BaseModel):
    text: str
    provider: str
    generated_at: str


class LLMSummaryResponse(BaseModel):
    state: AnalysisStatusResponse
    result: Optional[LLMSummaryResult] = None


class LLMAskRequest(BaseModel):
    question: str = Field(..., min_length=3, max_length=1000)
    provider: str = Field(default="free")


class LLMAskResponse(BaseModel):
    ok: bool
    answer: Optional[str] = None
    provider: Optional[str] = None
    error: Optional[str] = None


class QAHistoryItem(BaseModel):
    question: str
    answer: str
    provider: str
    timestamp: str


# ============================================================
# Endpoints
# ============================================================
@router.get(
    "/tasks/{task_id}/llm/providers",
    response_model=LLMProvidersResponse,
)
async def llm_providers(
    task_id: str,
    user: TelegramUser = Depends(get_current_user),
):
    """Возвращает статус доступности LLM-провайдеров."""
    await _require_done_task(task_id, user)
    return LLMProvidersResponse(**get_llm_providers_status())


@router.post(
    "/tasks/{task_id}/llm/summary",
    response_model=LLMSummaryResponse,
)
async def start_llm_summary_endpoint(
    task_id: str,
    request: LLMSummaryRequest,
    user: TelegramUser = Depends(get_current_user),
):
    """
    Запускает генерацию аналитического резюме через LLM.

    Длительная операция (15-60 сек в зависимости от провайдера):
      1. Расчёт сравнения метрик (current vs prev)
      2. Подготовка контекста (кросс-таблицы, очаги если есть)
      3. Запрос к нейросети

    Если уже выполнено с тем же провайдером — возвращает готовое.
    Если выполнено с другим провайдером — перезапускает.
    """
    task = await _require_done_task(task_id, user)

    if request.provider not in ("free", "paid"):
        raise HTTPException(
            status_code=400,
            detail="provider must be 'free' or 'paid'",
        )

    state = task.llm_summary_state

    # Если уже выполнено с тем же провайдером — возвращаем готовое
    if (
        state.status == AnalysisStatus.DONE
        and state.result
        and state.result.get("provider") == request.provider
    ):
        return LLMSummaryResponse(
            state=_state_to_response(state),
            result=LLMSummaryResult(**state.result),
        )

    # Если выполняется с тем же провайдером — возвращаем статус
    if (
        state.status == AnalysisStatus.RUNNING
        and state.result is None
    ):
        # Возможно, запущен с другим провайдером — проверим через stage
        # Простая логика: пусть выполняется до конца, потом можно перезапустить
        return LLMSummaryResponse(state=_state_to_response(state))

    # Перезапуск
    loop = asyncio.get_running_loop()
    loop.create_task(start_llm_summary(task, provider=request.provider))

    return LLMSummaryResponse(state=_state_to_response(state))


@router.get(
    "/tasks/{task_id}/llm/summary",
    response_model=LLMSummaryResponse,
)
async def get_llm_summary_status(
    task_id: str,
    wait: int = 0,
    user: TelegramUser = Depends(get_current_user),
):
    """
    Возвращает статус генерации LLM-резюме.

    Поддержка long polling: если ?wait=N (секунды) и статус running,
    endpoint держит соединение открытым до N секунд, ожидая завершения.
    Возвращает сразу при смене статуса на done/failed или по таймауту.
    """
    task = await _require_done_task(task_id, user)
    state = task.llm_summary_state

    # Long polling: ждём, пока статус running, до `wait` секунд.
    # time.monotonic() предпочтительнее asyncio.get_event_loop().time()
    # (тот устарел в Python 3.10+ и выдаёт DeprecationWarning).
    if wait > 0 and state.status == AnalysisStatus.RUNNING:
        deadline = time.monotonic() + min(wait, 60)
        while (
            state.status == AnalysisStatus.RUNNING
            and time.monotonic() < deadline
        ):
            await asyncio.sleep(1)

    return LLMSummaryResponse(
        state=_state_to_response(state),
        result=LLMSummaryResult(**state.result)
        if state.status == AnalysisStatus.DONE and state.result
        else None,
    )


@router.post(
    "/tasks/{task_id}/llm/ask",
    response_model=LLMAskResponse,
)
async def ask_llm(
    task_id: str,
    request: LLMAskRequest,
    user: TelegramUser = Depends(get_current_user),
):
    """
    Отвечает на вопрос пользователя по данным ДТП.

    Длительная операция (15-60 сек). Не использует state-машину —
    ответ возвращается сразу (когда нейросеть сгенерирует его).

    История вопросов сохраняется на задаче (последние 10).
    """
    task = await _require_done_task(task_id, user)

    if request.provider not in ("free", "paid"):
        raise HTTPException(
            status_code=400,
            detail="provider must be 'free' or 'paid'",
        )

    result = await ask_llm_question(
        task=task,
        question=request.question,
        provider=request.provider,
    )

    if not result.get("ok"):
        return LLMAskResponse(
            ok=False,
            error=result.get("error", "Неизвестная ошибка"),
        )

    return LLMAskResponse(
        ok=True,
        answer=result["answer"],
        provider=result.get("provider"),
    )


@router.get(
    "/tasks/{task_id}/llm/qa-history",
    response_model=List[QAHistoryItem],
)
async def get_qa_history(
    task_id: str,
    user: TelegramUser = Depends(get_current_user),
):
    """Возвращает историю вопросов/ответов LLM (последние 10)."""
    task = await _require_done_task(task_id, user)
    return [QAHistoryItem(**item) for item in task.llm_qa_history]
