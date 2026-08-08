"""
Роутер LLM-аналитики: providers + summary + Q&A.

Endpoints:
- GET  /api/dtp/tasks/{task_id}/llm/providers           — статус LLM-провайдеров
- POST /api/dtp/tasks/{task_id}/llm/summary             — запуск генерации резюме (async)
- GET  /api/dtp/tasks/{task_id}/llm/summary             — статус/результат резюме (long-poll)
- POST /api/dtp/tasks/{task_id}/llm/summary/stream      — SSE-стрим резюме (Sprint 4)
- POST /api/dtp/tasks/{task_id}/llm/ask                 — вопрос нейросети (sync)
- POST /api/dtp/tasks/{task_id}/llm/ask/stream          — SSE-стрим ответа (Sprint 4)
- GET  /api/dtp/tasks/{task_id}/llm/qa-history          — история вопросов/ответов

Все endpoints требуют готовую задачу (task.status == 'done').

Вынесено из routers/analyze.py (Sprint 3).
Sprint 4: добавлены /stream SSE-эндпоинты для progressive text reveal.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from ..services.gibdd_service import (
    AnalysisStatus,
    ask_llm_question,
    get_llm_providers_status,
    start_llm_summary,
)
from ..services.llm_ops import (
    ask_llm_question_stream,
    stream_llm_summary,
)
from ..telegram_auth import TelegramUser, get_current_user
from ._common import AnalysisStatusResponse, _require_done_task, _state_to_response

logger = logging.getLogger(__name__)

# Без prefix — analyze.py (facade) задаёт /dtp на агрегированном router.
router = APIRouter(tags=["analyze"])


# SSE heartbeat interval — каждые 15 сек эмитим ping,
# чтобы прокси (nginx, Cloudflare) не закрыл соединение по idle timeout.
# sse-starlette автоматически вставляет ping, пока генератор не yield'ит.
_SSE_PING_INTERVAL_SEC = 15

# SSE response headers — критично для streaming через прокси.
# X-Accel-Buffering: no — отключает буферизацию в nginx (и других прокси,
# которые уважают этот заголовок). Без него nginx буферизует весь ответ
# и отдаёт клиенту только после завершения стрима — пользователь видит
# "ничего не происходит" вместо token-by-token reveal.
# Cache-Control: no-cache — предотвращает кэширование SSE-ответов.
_SSE_HEADERS = {
    "X-Accel-Buffering": "no",
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
}

# ⚠️ Sprint 4 FIX: разделитель строк в SSE — "\n" (НЕ дефолтный "\r\n").
# sse_starlette по умолчанию использует "\r\n", из-за чего events разделяются
# "\r\n\r\n". Frontend (consumeSSE в api.ts) ищет "\n\n" через indexOf —
# и НЕ находит, потому что между двумя \n стоит \r. В итоге chunks
# накапливаются в buffer, но НЕ парсятся — пользователь видит ответ
# только после завершения стрима целиком. С sep="\n" events разделяются
# "\n\n", и frontend корректно парсит их в реальном времени.
_SSE_SEP = "\n"


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


# ============================================================
# Sprint 4: SSE streaming endpoints
# ============================================================
# Протокол SSE (Server-Sent Events):
#   Content-Type: text/event-stream
#   Формат события:
#       event: <event_name>\n
#       data: <payload>\n
#       \n  (пустая строка = конец события)
#
# Типы событий:
#   delta  — частичный текст (один или несколько токенов).
#            data = plain text (НЕ JSON) — фронтенд просто конкатенирует.
#   done   — стрим завершился успешно. data = полный текст.
#   error  — стрим оборвался или LLM вернул ошибку. data = JSON {"error": "..."}.
#   ping   — heartbeat, чтобы прокси не закрыл соединение. data = "".
#
# sse-starlette автоматически эмитит ping каждые ping_interval секунд.
# Мы добавляем свой ping из генератора, чтобы покрыть случай, когда
# LLM долго не отвечает (подготовка промпта 5-15 сек).


@router.post(
    "/tasks/{task_id}/llm/ask/stream",
)
async def ask_llm_stream(
    task_id: str,
    request: LLMAskRequest,
    user: TelegramUser = Depends(get_current_user),
):
    """
    SSE-стрим ответа на вопрос пользователя.

    Возвращает text/event-stream с событиями delta/done/error/ping.
    Фронтенд конкатенирует delta-события и показывает текст по мере поступления.

    В отличие от POST /llm/ask (блокирует 15-60 сек и возвращает JSON),
    этот endpoint начинает отдавать токены сразу, как только LLM их прислал —
    UX становится интерактивным (ChatGPT-style).

    sse-starlette автоматически эмитит ping-события каждые _SSE_PING_INTERVAL_SEC
    секунд, пока генератор не yield'ит — это покрывает фазу подготовки промпта
    (ensure_cards, ensure_comparison, cross_tables — до 15 сек).
    """
    task = await _require_done_task(task_id, user)

    if request.provider not in ("free", "paid"):
        raise HTTPException(
            status_code=400,
            detail="provider must be 'free' or 'paid'",
        )

    question = request.question
    provider = request.provider

    async def event_generator():
        """SSE-генератор: эмитит delta/done/error события."""
        try:
            chunks_emitted = 0
            async for delta in ask_llm_question_stream(
                task=task, question=question, provider=provider,
            ):
                chunks_emitted += 1
                yield {"event": "delta", "data": delta}
            # Стрим завершился нормально — эмитим done.
            # Фронтенд уже накопил текст из delta-событий.
            if chunks_emitted == 0:
                # LLM вернул пустой ответ (0 chunks) — эмитим error,
                # чтобы фронтенд показал сообщение, а не "тихо" завершился.
                logger.warning(
                    f"Task {task_id}: LLM ask stream — empty response "
                    f"(0 chunks), provider={provider}"
                )
                yield {"event": "error", "data": json.dumps({
                    "error": "LLM вернул пустой ответ. Попробуйте переформулировать вопрос или сменить провайдера.",
                })}
            else:
                yield {"event": "done", "data": ""}
        except asyncio.CancelledError:
            # Клиент отключился (AbortController во фронтенде)
            logger.info(
                f"Task {task_id}: LLM ask stream cancelled by client"
            )
            raise
        except Exception as exc:
            err_msg = str(exc)[:500]
            logger.warning(
                f"Task {task_id}: LLM ask stream error: {err_msg}"
            )
            yield {"event": "error", "data": json.dumps({
                "error": err_msg,
            })}

    return EventSourceResponse(
        event_generator(),
        ping=_SSE_PING_INTERVAL_SEC,
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
        sep=_SSE_SEP,  # Sprint 4 FIX: "\n" вместо дефолтного "\r\n"
    )


@router.post(
    "/tasks/{task_id}/llm/summary/stream",
)
async def llm_summary_stream(
    task_id: str,
    request: LLMSummaryRequest,
    user: TelegramUser = Depends(get_current_user),
):
    """
    SSE-стрим генерации аналитического резюме.

    Возвращает text/event-stream с событиями delta/done/error/ping.

    В отличие от связки POST /llm/summary + GET /llm/summary?wait=N
    (long-polling), этот endpoint:
      - Если есть cache hit — эмитит весь текст одним delta и done (мгновенно).
      - Если cache miss — стримит из LLM token-by-token.

    Прогресс (state.progress) обновляется на сервере, но фронтенду он
    не нужен — пользователь видит растущий текст.
    """
    task = await _require_done_task(task_id, user)

    if request.provider not in ("free", "paid"):
        raise HTTPException(
            status_code=400,
            detail="provider must be 'free' or 'paid'",
        )

    provider = request.provider

    async def event_generator():
        try:
            chunks_emitted = 0
            async for delta in stream_llm_summary(task=task, provider=provider):
                chunks_emitted += 1
                yield {"event": "delta", "data": delta}
            if chunks_emitted == 0:
                logger.warning(
                    f"Task {task_id}: LLM summary stream — empty response "
                    f"(0 chunks), provider={provider}"
                )
                yield {"event": "error", "data": json.dumps({
                    "error": "LLM вернул пустой ответ. Попробуйте ещё раз или смените провайдера.",
                })}
            else:
                yield {"event": "done", "data": ""}
        except asyncio.CancelledError:
            logger.info(
                f"Task {task_id}: LLM summary stream cancelled by client"
            )
            raise
        except Exception as exc:
            err_msg = str(exc)[:500]
            logger.warning(
                f"Task {task_id}: LLM summary stream error: {err_msg}"
            )
            yield {"event": "error", "data": json.dumps({
                "error": err_msg,
            })}

    return EventSourceResponse(
        event_generator(),
        ping=_SSE_PING_INTERVAL_SEC,
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
        sep=_SSE_SEP,  # Sprint 4 FIX: "\n" вместо дефолтного "\r\n"
    )
