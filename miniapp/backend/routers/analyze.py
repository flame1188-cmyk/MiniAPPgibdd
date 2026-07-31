"""
Роутер для аналитических операций: очаги, статистика по точке, LLM-анализ.

Все endpoints требуют готовую задачу (task.status == 'done').
Карточки ДТП уже сохранены на task.cards после execute_task.

Endpoints:
- GET  /api/dtp/tasks/{task_id}/llm/providers  — статус LLM-провайдеров
- POST /api/dtp/tasks/{task_id}/clusters        — запуск расчёта очагов (async)
- GET  /api/dtp/tasks/{task_id}/clusters        — статус/результат очагов
- GET  /api/dtp/tasks/{task_id}/clusters/map    — HTML-карта очагов (iframe)
- GET  /api/dtp/tasks/{task_id}/clusters/excel  — Excel-файл очагов (4 листа)
- POST /api/dtp/tasks/{task_id}/point           — статистика по точке (sync)
- GET  /api/dtp/tasks/{task_id}/point/excel     — Excel статистики по точке
- GET  /api/dtp/tasks/{task_id}/point/map       — HTML-карта точки (iframe)
- POST /api/dtp/tasks/{task_id}/llm/summary     — запуск генерации резюме (async)
- GET  /api/dtp/tasks/{task_id}/llm/summary     — статус/результат резюме
- POST /api/dtp/tasks/{task_id}/llm/ask         — вопрос нейросети (sync)
- GET  /api/dtp/tasks/{task_id}/llm/qa-history  — история вопросов/ответов
"""
from __future__ import annotations

import asyncio
import logging
import re
import urllib.parse
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from ..services.gibdd_service import (
    AnalysisStatus,
    Task,
    TaskStatus,
    ask_llm_question,
    compute_point_stats,
    generate_clusters_excel,
    generate_clusters_map_html,
    generate_point_stats_excel,
    generate_point_stats_map_html,
    get_llm_providers_status,
    get_task,
    start_clusters_calculation,
    start_llm_summary,
)
from ..telegram_auth import TelegramUser, get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/dtp", tags=["analyze"])


# ============================================================
# Helpers
# ============================================================
def _require_done_task(task_id: str, user: TelegramUser) -> Task:
    """
    Проверяет, что задача принадлежит пользователю и завершена.
    Возвращает task или raises HTTPException.
    """
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.user_id != user.id:
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


# ============================================================
# Schemas
# ============================================================
class AnalysisStatusResponse(BaseModel):
    """Статус длительной операции (очаги/LLM-резюме)."""
    status: str  # idle | running | done | failed
    progress: int
    stage: str
    error: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


class ClustersSummary(BaseModel):
    total_clusters: int
    total_lost: int
    total_preclusters: int
    current_total_dtp: int
    current_deaths: int
    current_injured: int
    dynamics: Dict[str, int]
    has_prev_data: bool
    prev_label: Optional[str] = None
    current_label: str
    region_name: str


class ClusterItem(BaseModel):
    road: str
    zone_type: str
    total_accidents: int
    deaths: int
    injured: int
    # None означает "смешанный тип" — 5+ ДТП разных видов без явного доминанта
    dominant_type: Optional[str] = None
    type_counter: Dict[str, int]
    center: Optional[Dict[str, float]] = None
    start_pos: Optional[float] = None
    end_pos: Optional[float] = None
    dates: List[str] = []
    dynamics: Dict[str, Any] = {}
    camera_match: Optional[Dict[str, Any]] = None


class ClustersResult(BaseModel):
    summary: ClustersSummary
    clusters: List[ClusterItem]
    preclusters: List[ClusterItem]


class ClustersResponse(BaseModel):
    """Ответ POST /clusters и GET /clusters."""
    state: AnalysisStatusResponse
    result: Optional[ClustersResult] = None


class PointRequest(BaseModel):
    lat: float = Field(..., ge=-90, le=90, description="Широта")
    lon: float = Field(..., ge=-180, le=180, description="Долгота")
    radius_m: int = Field(
        default=500, gt=0, le=10000,
        description="Радиус в метрах (рекомендуется: 250, 500, 1000, 3000)",
    )


class PointPeriodStats(BaseModel):
    total: int
    deaths: int
    injured: int
    alcohol: int
    pedestrians: int
    by_type: Dict[str, int]
    by_road: Dict[str, int]
    by_weather: Dict[str, int]
    cards_count: int
    cards_preview: List[Dict[str, Any]] = []


class PointStatsResponse(BaseModel):
    ok: bool
    center: Dict[str, float]
    radius_m: int
    current_label: str
    prev_label: Optional[str] = None
    current: Optional[PointPeriodStats] = None
    prev: Optional[PointPeriodStats] = None
    error: Optional[str] = None


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
# Endpoints: LLM providers
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
    _require_done_task(task_id, user)
    return LLMProvidersResponse(**get_llm_providers_status())


# ============================================================
# Endpoints: Clusters (очаги)
# ============================================================
@router.post(
    "/tasks/{task_id}/clusters",
    response_model=ClustersResponse,
)
async def start_clusters(
    task_id: str,
    user: TelegramUser = Depends(get_current_user),
):
    """
    Запускает асинхронный расчёт очагов концентрации ДТП.

    Длительная операция (15-30 сек):
      1. Загрузка границ НП из OpenStreetMap
      2. Классификация ДТП (в НП / вне НП)
      3. Кластеризация по радиусу + пикетажу
      4. Сопоставление с прошлым годом (динамика)
      5. Обогащение камерами (если есть)

    Повторный вызов возвращает текущий статус.
    Если расчёт уже выполнен — возвращает готовый результат без пересчёта.
    """
    task = _require_done_task(task_id, user)
    state = task.clusters_state

    # Если уже выполнено — возвращаем готовое
    if state.status == AnalysisStatus.DONE:
        return ClustersResponse(
            state=_state_to_response(state),
            result=_clusters_result_to_response(state.result),
        )

    # Если уже выполняется — возвращаем статус
    if state.status == AnalysisStatus.RUNNING:
        return ClustersResponse(state=_state_to_response(state))

    # Если предыдущая попытка упала — перезапускаем
    # Запускаем async
    loop = asyncio.get_running_loop()
    loop.create_task(start_clusters_calculation(task))

    return ClustersResponse(state=_state_to_response(state))


@router.get(
    "/tasks/{task_id}/clusters",
    response_model=ClustersResponse,
)
async def get_clusters_status(
    task_id: str,
    user: TelegramUser = Depends(get_current_user),
):
    """
    Возвращает статус расчёта очагов (для polling из frontend).
    """
    task = _require_done_task(task_id, user)
    state = task.clusters_state
    return ClustersResponse(
        state=_state_to_response(state),
        result=_clusters_result_to_response(state.result)
        if state.status == AnalysisStatus.DONE
        else None,
    )


@router.get(
    "/tasks/{task_id}/clusters/map",
    response_class=HTMLResponse,
)
async def get_clusters_map(
    task_id: str,
    user: TelegramUser = Depends(get_current_user),
):
    """
    Отдаёт HTML-карту очагов (Leaflet с маркерами).
    Используется в <iframe> на frontend.

    Полноценная карта из Telegram-бота:
    - Слои (Очаги / ДТП в очагах / Предочаги / Камеры)
    - Popups на ДТП и очагах с детальной информацией
    - Линейка для измерения расстояний
    - Convex hull (зона очага)
    - Динамика (новые/рост/снижение/стабильный/исчезнувший)
    - Фильтр камер по моделям
    """
    task = _require_done_task(task_id, user)
    if task.clusters_state.status != AnalysisStatus.DONE:
        raise HTTPException(
            status_code=404,
            detail="Clusters not calculated yet. Call POST /clusters first.",
        )
    html = await generate_clusters_map_html(task)
    if not html:
        raise HTTPException(status_code=500, detail="Map generation failed")
    return HTMLResponse(content=html)


@router.get(
    "/tasks/{task_id}/clusters/excel",
)
async def get_clusters_excel(
    task_id: str,
    user: TelegramUser = Depends(get_current_user),
):
    """
    Скачивает Excel-файл с очагами ДТП (4 листа):
      Лист 1 «Очаги ДТП» — текущие очаги (с цветовым кодированием зоны)
      Лист 2 «Динамика очагов» — текущие + исчезнувшие со статусом
      Лист 3 «Детализация ДТП» — все ДТП по периодам
      Лист 4 «Предочаги» — места, не дотянувшие до очага

    Content-Disposition: attachment; filename="dtp_ochagi_<регион>_<период>.xlsx"
    """
    task = _require_done_task(task_id, user)
    if task.clusters_state.status != AnalysisStatus.DONE:
        raise HTTPException(
            status_code=404,
            detail="Clusters not calculated yet. Call POST /clusters first.",
        )

    xlsx_bytes = await generate_clusters_excel(task)
    if not xlsx_bytes:
        raise HTTPException(
            status_code=500,
            detail="Excel generation failed",
        )

    # Безопасное имя файла (RFC 5987: ASCII-fallback + UTF-8 form)
    # Cyrillic в filename= ломает starlette (latin-1 encode),
    # поэтому ASCII fallback + filename*=UTF-8''<urlencoded>
    safe_reg_ascii = re.sub(
        r"[^A-Za-z0-9_-]", "_", task.region_name[:30]
    ).strip("_") or "region"
    safe_period_ascii = re.sub(
        r"[^A-Za-z0-9_-]", "_", task.period_label[:30]
    ).strip("_") or "period"
    filename_ascii = f"dtp_ochagi_{safe_reg_ascii}_{safe_period_ascii}.xlsx"
    # Полное имя с кириллицей для современных клиентов
    filename_full = f"dtp_ochagi_{task.region_name}_{task.period_label}.xlsx"
    filename_utf8 = urllib.parse.quote(filename_full, safe="")

    return Response(
        content=xlsx_bytes,
        media_type=(
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        ),
        headers={
            "Content-Disposition": (
                f'attachment; filename="{filename_ascii}"; '
                f"filename*=UTF-8''{filename_utf8}"
            ),
        },
    )


# ============================================================
# Endpoints: Point statistics
# ============================================================
@router.post(
    "/tasks/{task_id}/point",
    response_model=PointStatsResponse,
)
async def compute_point_statistics(
    task_id: str,
    request: PointRequest,
    user: TelegramUser = Depends(get_current_user),
):
    """
    Считает статистику ДТП в радиусе от точки.

    Быстрая операция (<1 сек): фильтрация карточек по радиусу
    через формулу Гаверсинуса.

    Автоматически загружает данные за прошлый год (если ещё нет)
    для сравнения динамики.
    """
    task = _require_done_task(task_id, user)
    result = await compute_point_stats(
        task=task,
        lat=request.lat,
        lon=request.lon,
        radius_m=request.radius_m,
    )
    if not result.get("ok"):
        return PointStatsResponse(
            ok=False,
            center={"lat": request.lat, "lon": request.lon},
            radius_m=request.radius_m,
            current_label=task.period_label,
            error=result.get("error", "Неизвестная ошибка"),
        )

    return PointStatsResponse(
        ok=True,
        center=result["center"],
        radius_m=result["radius_m"],
        current_label=result["current_label"],
        prev_label=result.get("prev_label"),
        current=result.get("current"),
        prev=result.get("prev"),
    )


@router.get(
    "/tasks/{task_id}/point/excel",
)
async def get_point_stats_excel(
    task_id: str,
    user: TelegramUser = Depends(get_current_user),
):
    """
    Скачивает Excel-файл со статистикой по точке (2 листа):
      Лист 1 — текущий период (все ДТП в радиусе с детальной информацией)
      Лист 2 — прошлый период (если есть)

    Требует предварительно выполненный POST /point — берёт карточки из кэша задачи.

    Content-Disposition: attachment; filename="point_stats_<регион>_<период>.xlsx"
    """
    task = _require_done_task(task_id, user)

    if not task.last_point_cards_current and not task.last_point_cards_prev:
        raise HTTPException(
            status_code=404,
            detail=(
                "Point statistics not calculated yet. "
                "Call POST /point first with lat/lon/radius_m."
            ),
        )

    xlsx_bytes = await generate_point_stats_excel(task)
    if not xlsx_bytes:
        raise HTTPException(
            status_code=500,
            detail="Excel generation failed",
        )

    # Безопасное имя файла (RFC 5987: ASCII-fallback + UTF-8 form)
    safe_reg_ascii = re.sub(
        r"[^A-Za-z0-9_-]", "_", task.region_name[:30]
    ).strip("_") or "region"
    params = task.last_point_params or {}
    lat_str = f"{params.get('lat', 0):.4f}".replace(".", "-")
    lon_str = f"{params.get('lon', 0):.4f}".replace(".", "-")
    radius = int(params.get("radius_m", 0))
    filename_ascii = (
        f"point_stats_{safe_reg_ascii}_{lat_str}_{lon_str}_{radius}m.xlsx"
    )
    # Полное имя с кириллицей для современных клиентов
    filename_full = (
        f"point_stats_{task.region_name}_{lat_str}_{lon_str}_{radius}m.xlsx"
    )
    filename_utf8 = urllib.parse.quote(filename_full, safe="")

    return Response(
        content=xlsx_bytes,
        media_type=(
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        ),
        headers={
            "Content-Disposition": (
                f'attachment; filename="{filename_ascii}"; '
                f"filename*=UTF-8''{filename_utf8}"
            ),
        },
    )


@router.get(
    "/tasks/{task_id}/point/map",
    response_class=HTMLResponse,
)
async def get_point_stats_map(
    task_id: str,
    lat: float = Query(..., ge=-90, le=90, description="Широта"),
    lon: float = Query(..., ge=-180, le=180, description="Долгота"),
    radius_m: int = Query(
        default=500, gt=0, le=10000,
        description="Радиус в метрах",
    ),
    user: TelegramUser = Depends(get_current_user),
):
    """
    Отдаёт HTML-карту статистики по точке (Leaflet в iframe).

    Карта: точка запроса + круг радиуса + ДТП (текущий/прошлый) +
    камеры в радиусе. С попапами на каждой точке.
    """
    task = _require_done_task(task_id, user)

    html = await generate_point_stats_map_html(task, lat, lon, radius_m)
    if not html:
        raise HTTPException(
            status_code=500,
            detail="Map generation failed",
        )
    return HTMLResponse(content=html)


# ============================================================
# Endpoints: LLM summary
# ============================================================
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
    task = _require_done_task(task_id, user)

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
    user: TelegramUser = Depends(get_current_user),
):
    """Возвращает статус генерации LLM-резюме (для polling)."""
    task = _require_done_task(task_id, user)
    state = task.llm_summary_state
    return LLMSummaryResponse(
        state=_state_to_response(state),
        result=LLMSummaryResult(**state.result)
        if state.status == AnalysisStatus.DONE and state.result
        else None,
    )


# ============================================================
# Endpoints: LLM Q&A
# ============================================================
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
    task = _require_done_task(task_id, user)

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
    task = _require_done_task(task_id, user)
    return [QAHistoryItem(**item) for item in task.llm_qa_history]


# ============================================================
# Helpers
# ============================================================
def _state_to_response(state) -> AnalysisStatusResponse:
    """Преобразует AnalysisState в AnalysisStatusResponse."""
    return AnalysisStatusResponse(
        status=state.status.value if hasattr(state.status, "value") else str(state.status),
        progress=state.progress,
        stage=state.stage,
        error=state.error,
        started_at=state.started_at.isoformat() if state.started_at else None,
        finished_at=state.finished_at.isoformat() if state.finished_at else None,
    )


def _clusters_result_to_response(result: Optional[dict]) -> Optional[ClustersResult]:
    """Преобразует результат в ClustersResult."""
    if not result:
        return None

    summary = ClustersSummary(
        total_clusters=result.get("total_clusters", 0),
        total_lost=result.get("total_lost", 0),
        total_preclusters=result.get("total_preclusters", 0),
        current_total_dtp=result.get("current_total_dtp", 0),
        current_deaths=result.get("current_deaths", 0),
        current_injured=result.get("current_injured", 0),
        dynamics=result.get("dynamics", {}),
        has_prev_data=result.get("has_prev_data", False),
        prev_label=result.get("prev_label"),
        current_label=result.get("current_label", ""),
        region_name=result.get("region_name", ""),
    )

    clusters = [ClusterItem(**c) for c in result.get("clusters", [])]
    preclusters = [ClusterItem(**p) for p in result.get("preclusters", [])]

    return ClustersResult(summary=summary, clusters=clusters, preclusters=preclusters)
