"""
Основной роутер: создание задач выгрузки, опрос статуса, скачивание файлов.

Архитектура endpoint'ов:
- POST /api/dtp/tasks              — создать задачу (async выполнение в фоне)
- GET  /api/dtp/tasks              — список задач пользователя
- GET  /api/dtp/tasks/{id}         — статус задачи (для polling)
- GET  /api/dtp/tasks/{id}/map     — HTML-карта (генерируется лениво при первом запросе)
- GET  /api/dtp/tasks/{id}/map-data — JSON-данные карты для переключения года
- POST /api/dtp/tasks/{id}/generate-excel — ленивая генерация Excel для существующей задачи
- GET  /api/dtp/tasks/{id}/download/{file_type} — скачать ранее сгенерированный файл
- POST /api/dtp/export-only        — выгрузка файлов без аналитики (отдельная вкладка)
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from ..services.gibdd_service import (
    Task,
    TaskStatus,
    create_task,
    execute_task,
    get_task_async,
    list_user_tasks,
    parse_user_query,
)
from ..telegram_auth import TelegramUser, get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dtp", tags=["dtp"])


# ============================================================
# Schemas
# ============================================================
class TaskCreateRequest(BaseModel):
    """Запрос на создание задачи выгрузки.

    Поддерживает два режима:
    1. Structured (рекомендуется): регион + период выбраны из списка.
       Поля region_code, region_name, dat_list, period_label заполнены.
       Парсинг текста не выполняется — ошибок распознавания нет.
    2. Text (legacy): произвольный текст в `query`.
       Парсится через user_request_parser.

    Если region_code + dat_list заполнены → structured mode,
    иначе — text mode (тогда `query` обязателен).
    """

    query: Optional[str] = Field(
        default=None, max_length=500,
        description="Текстовый запрос (legacy-режим). "
                    "Игнорируется, если заданы region_code и dat_list.",
    )
    region_code: Optional[str] = Field(
        default=None,
        description="Код региона (например '1101'). Structured-режим.",
    )
    region_name: Optional[str] = Field(
        default=None,
        description="Название региона для отображения. Structured-режим.",
    )
    dat_list: Optional[List[str]] = Field(
        default=None,
        description="Список месяцев в формате 'M.YYYY' (например ['1.2025', '2.2025']).",
    )
    period_label: Optional[str] = Field(
        default=None,
        description="Человекочитаемая метка периода (например '2025 год').",
    )


class TaskCreateResponse(BaseModel):
    task_id: str
    status: TaskStatus
    region_code: str
    region_name: str
    period: str


class TaskFileSchema(BaseModel):
    # task.files содержит также "path", которого нет в схеме — игнорируем лишнее
    model_config = ConfigDict(extra="ignore")

    type: str
    filename: str
    size_bytes: int
    mime: str


class TaskStatusResponse(BaseModel):
    task_id: str
    status: TaskStatus
    progress: int
    region_code: str
    region_name: str
    period: str
    total_dtp: int = 0
    total_dead: int = 0
    total_injured: int = 0
    error: Optional[str] = None
    files: List[TaskFileSchema] = []
    analytics: Optional[Dict[str, Any]] = None
    # N6: backpressure — очередь перед semaphore
    queue_position: Optional[int] = None
    queue_ahead: Optional[int] = None


class ExportOnlyRequest(BaseModel):
    """Запрос на выгрузку файлов без аналитики (отдельная вкладка)."""
    region_code: str
    region_name: str
    dat_list: List[str]
    period_label: str


class GenerateExcelRequest(BaseModel):
    """Запрос на ленивую генерацию Excel для существующей задачи."""
    file_type: str = Field(
        ..., description="'dtp_cards' или 'dtp_participants'"
    )


class CompareYearRequest(BaseModel):
    """Запрос пересчёта сравнения с другим годом."""
    compare_year: Optional[int] = Field(
        default=None,
        description="Год для сравнения. None = АППГ (year-1).",
    )
    force_refresh: bool = Field(
        default=False,
        description="Принудительно пересчитать current_metrics (сбросить in-memory кэш).",
    )


# ============================================================
# Endpoints
# ============================================================
@router.post("/tasks", response_model=TaskCreateResponse)
async def create_dtp_task(
    request: TaskCreateRequest,
    user: TelegramUser = Depends(get_current_user),
):
    """
    Создаёт задачу выгрузки ДТП и запускает её асинхронно (через asyncio).

    Два режима:
    - Structured: заполнены region_code + dat_list (+ region_name, period_label).
      Парсинг текста не выполняется.
    - Text (legacy): только query. Парсится через user_request_parser.
    """
    # === Определяем режим ===
    is_structured = bool(
        request.region_code
        and request.dat_list
        and len(request.dat_list) > 0
    )

    if is_structured:
        # Structured mode — без парсинга
        region_code = request.region_code or ""
        region_name = request.region_name or f"Регион {region_code}"
        dat_list = request.dat_list or []
        period_label = request.period_label or (
            f"{len(dat_list)} мес." if dat_list else "—"
        )
        raw_query = f"[structured] {region_name} | {period_label} | {dat_list}"
    else:
        # Text mode — парсим через user_request_parser
        if not request.query or len(request.query.strip()) < 2:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "Укажите либо region_code + dat_list (structured mode), "
                    "либо query длиной минимум 2 символа (text mode)."
                ),
            )
        parsed = await parse_user_query(request.query)
        if not parsed.get("ok"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=parsed.get("error", "Не удалось распознать запрос"),
            )
        region_code = parsed["region_code"]
        region_name = parsed["region_name"]
        dat_list = parsed["dat_list"]
        period_label = parsed["period"]
        raw_query = request.query

    task = await create_task(
        user_id=user.id,
        region_code=region_code,
        region_name=region_name,
        period_label=period_label,
        dat_list=dat_list,
        raw_query=raw_query,
    )

    # Hotfix Sprint 7: гарантированно persist'им метаданные задачи в БД
    # ДО запуска execute_task.
    try:
        from ..db.repository import save_task
        await save_task(task)
    except Exception as exc:
        logging.getLogger(__name__).warning(
            f"create_dtp_task: persist task_id={task.id} failed: {exc} "
            f"(задача доступна in-memory, но может быть потеряна при рестарте)"
        )

    # Аудит обращения к ПДн (152-ФЗ)
    try:
        from ..db.repository import log_access
        await log_access(
            user_id=user.id,
            action="create_task",
            region_code=region_code,
            period_label=period_label,
            task_id=task.id,
        )
    except Exception:
        pass

    # Асинхронный запуск в фоне
    _asyncio_create_task(task.id)

    return TaskCreateResponse(
        task_id=task.id,
        status=task.status,
        region_code=task.region_code,
        region_name=task.region_name,
        period=task.period_label,
    )


def _asyncio_create_task(task_id: str) -> None:
    """Запускает pipeline через dispatcher."""
    from ..services.gibdd_service import get_task_async
    from worker.dispatcher import dispatch_execute_pipeline

    async def _dispatch():
        task = await get_task_async(task_id)
        if task is None:
            from ..services.gibdd_service import execute_task
            await execute_task(task_id)
            return

        dispatch_execute_pipeline(
            task_id=task.id,
            dat_list=task.dat_list,
            reg_code=task.region_code,
            region_name=task.region_name,
            period_label=task.period_label,
            prev_dat_list=None,
            prev_label=task.prev_label,
            user_id=task.user_id,
            raw_query=task.raw_query,
        )

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(_dispatch())
        return

    loop.create_task(_dispatch())


@router.get("/tasks", response_model=List[TaskStatusResponse])
async def list_tasks(
    user: TelegramUser = Depends(get_current_user),
    limit: int = 20,
):
    """Возвращает последние N задач пользователя."""
    tasks = await list_user_tasks(user.id, limit=limit)
    return [_task_to_response(t) for t in tasks]


@router.get("/tasks/{task_id}", response_model=TaskStatusResponse)
async def get_task_status(
    task_id: str,
    user: TelegramUser = Depends(get_current_user),
):
    """Возвращает статус задачи (для polling из frontend)."""
    task = await get_task_async(task_id)
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task {task_id} not found",
        )
    if task.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied",
        )
    return _task_to_response(task)


@router.delete("/tasks/{task_id}")
async def delete_task_endpoint(
    task_id: str,
    user: TelegramUser = Depends(get_current_user),
):
    """Удаляет задачу пользователя."""
    from ..db.repository import delete_task

    task = await get_task_async(task_id)
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task {task_id} not found",
        )
    if task.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied",
        )

    deleted = await delete_task(task_id, user.id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task {task_id} not found",
        )

    try:
        from ..db.repository import log_access
        await log_access(
            user_id=user.id,
            action="delete_task",
            region_code=task.region_code,
            period_label=task.period_label,
            task_id=task_id,
        )
    except Exception:
        pass

    return {"ok": True, "task_id": task_id, "deleted": True}


# ============================================================
# HTML-карта (ленивая генерация при первом запросе)
# ============================================================
@router.get("/tasks/{task_id}/map", response_class=HTMLResponse)
async def get_task_map(
    task_id: str,
    refresh: bool = Query(False, description="Перегенерировать карту и перезагрузить данные из ГИБДД"),
    user: TelegramUser = Depends(get_current_user),
):
    """
    Отдаёт HTML-карту (inline Leaflet с кластеризацией).
    Генерируется лениво при первом запросе и кэшируется в task.files.
    При refresh=True — удаляет кэш карты и перезагружает карточки из ГИБДД.
    """
    task = await get_task_async(task_id)
    if not task or task.user_id != user.id:
        raise HTTPException(status_code=404, detail="Task not found")

    # При refresh=True — инвалидируем кэш, перезагружаем данные
    if refresh:
        # 1. Удаляем кэшированный файл карты
        task.files = [f for f in task.files if f.get("type") != "map_html"]
        # 2. Инвалидируем cards_cache для этой задачи
        try:
            from ..db.cards_cache import invalidate_entry
            removed = await invalidate_entry(task.region_code, task.dat_list)
            if removed:
                logger.info(f"Task {task.id}: cards_cache invalidated ({removed} entries)")
        except Exception as exc:
            logger.warning(f"Task {task.id}: cards_cache invalidate failed: {exc}")
        # 3. Сбрасываем аналитику (пересчитается при следующем запросе)
        task.cross_tables = None
        task.cross_tables_cards_id = None
        task.current_metrics = None
        task.current_metrics_cards_id = None
        task.comparison = None
        task.analytics = None
        task.cards = []  # Принудительно пустые — ensure_cards перезагрузит
    else:
        # Ищем уже сгенерированную карту
        map_file = next(
            (f for f in task.files if f["type"] == "map_html"),
            None,
        )
        if map_file:
            path = Path(map_file["path"])
            if path.exists():
                return HTMLResponse(content=path.read_text(encoding="utf-8"))

    # Карта ещё не сгенерирована — генерируем лениво
    if not task.cards:
        # Восстанавливаем карточки из кэша
        from ..services.pipeline import ensure_cards
        result = await ensure_cards(task)
        if not result.get("ok"):
            raise HTTPException(
                status_code=503,
                detail=result.get("error", "Карточки не загружены"),
            )

    html_content = await _generate_map_html(task)
    if html_content is None:
        raise HTTPException(
            status_code=500, detail="Не удалось сгенерировать карту"
        )

    return HTMLResponse(content=html_content)


# ============================================================
# JSON-данные карты для переключения года
# ============================================================
@router.get("/tasks/{task_id}/map-data")
async def get_map_data(
    task_id: str,
    year: int = Query(..., description="Год для загрузки данных (полный год, 12 месяцев)"),
    user: TelegramUser = Depends(get_current_user),
):
    """Возвращает JSON с данными карты за указанный год.

    Используется фронтендом для переключения года на карте ДТП
    без перезагрузки страницы. Возвращает:
    - dtp_geojson: GeoJSON FeatureCollection с маркерами ДТП
    - pap_data: массив точек ПАП с нарушениями по статьям КоАП
    - summary: {total, deaths, injured}

    Данные загружаются за ПОЛНЫЙ год (январь-декабрь),
    независимо от периода исходной задачи.
    """
    task = await get_task_async(task_id)
    if not task or task.user_id != user.id:
        raise HTTPException(status_code=404, detail="Task not found")

    reg_code = task.region_code

    # Формируем dat_list на полный год (1-12 месяцев)
    year_dat_list = [f"{m}.{year}" for m in range(1, 13)]

    from ..db.connection import get_pool, is_db_ready

    if not is_db_ready():
        raise HTTPException(
            status_code=503,
            detail="База данных недоступна для загрузки данных за другой год",
        )

    pool = get_pool()
    if pool is None:
        raise HTTPException(
            status_code=503,
            detail="База данных недоступна",
        )

    try:
        # 1. DTP: SQL-запросы (сводка + карточки)
        dtp_rows, summary = await _fetch_map_data_from_db(
            pool, reg_code, year_dat_list
        )
        # 2. CPU-bound обработка (построение GeoJSON + попапов)
        dtp_geojson = await asyncio.to_thread(
            _build_dtp_geojson, dtp_rows
        )
        # 3. ПАП из отдельной таблицы pap_points
        from ..db.pap_repository import fetch_pap_for_map
        pap_data = await fetch_pap_for_map(reg_code, year_dat_list)
    except Exception as exc:
        logger.exception(f"get_map_data({task_id}, year={year}) failed")
        raise HTTPException(
            status_code=500,
            detail=f"Ошибка загрузки данных: {exc}",
        )

    return {
        "dtp_geojson": dtp_geojson,
        "pap_data": pap_data,
        "summary": summary,
    }


async def _fetch_map_data_from_db(
    pool, reg_code: str, dat_list: list[str]
) -> tuple[list[dict], dict]:
    """Асинхронно выполняет SQL-запросы для DTP (сводка + карточки).

    Возвращает (dtp_rows, summary).
    ПАП загружается отдельно через pap_repository.fetch_pap_for_map().
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            # Сводка за полный год
            await cur.execute("""
                SELECT COUNT(*) AS total,
                       COALESCE(SUM(pog), 0) AS deaths,
                       COALESCE(SUM(ran), 0) AS injured
                FROM gibdd_cards
                WHERE reg_code = %(reg)s AND dat_period = ANY(%(dats)s)
            """, {"reg": reg_code, "dats": dat_list})
            row = await cur.fetchone()
            summary = {
                "total": int(row["total"]),
                "deaths": int(row["deaths"]),
                "injured": int(row["injured"]),
            }

            # Карточки с координатами
            await cur.execute("""
                SELECT id, coord_w, coord_l, date_dtp, time,
                       dtpv, pog, ran, k_ts, empt_number,
                       dor, km, m, np, street, house, district,
                       raw_payload
                FROM gibdd_cards
                WHERE reg_code = %(reg)s
                  AND dat_period = ANY(%(dats)s)
                  AND coord_w IS NOT NULL AND coord_w != 0
                  AND coord_l IS NOT NULL AND coord_l != 0
                ORDER BY date_dtp
            """, {"reg": reg_code, "dats": dat_list})
            dtp_rows = await cur.fetchall()

    return dtp_rows, summary


def _build_dtp_geojson(
    dtp_rows: list[dict]
) -> dict:
    """CPU-bound: строит GeoJSON с попапами из строк карточек ДТП.

    Вызывается через asyncio.to_thread() для неблокирующей обработки.
    ПАП обрабатывается отдельно через pap_repository.fetch_pap_for_map().
    """
    from report_generator import _severity_color, _card_popup_html

    features = []
    for r in dtp_rows:
        lat = float(r["coord_w"])
        lon = float(r["coord_l"])

        # Восстанавливаем карточку-словарь для popup
        card = dict(r)
        card["date_dtp"] = r["date_dtp"].isoformat() if r["date_dtp"] else ""
        card["time"] = r["time"].isoformat() if r["time"] else ""

        # Извлекаем ts_info и dor_usl из raw_payload для popup
        raw = r.get("raw_payload") or {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                raw = {}
        card["ts_info"] = raw.get("ts_info", [])
        card["dor_usl"] = raw.get("dor_usl", {})

        popup_html = _card_popup_html(card)

        pog = int(r["pog"] or 0)
        ran = int(r["ran"] or 0)
        if pog > 0:
            severity = "fatal"
        elif ran > 0:
            severity = "injured"
        else:
            severity = "material"

        date_dtp = r["date_dtp"]
        date_sort = date_dtp.strftime("%Y%m%d") if date_dtp else ""

        features.append({
            "type": "Feature",
            "properties": {
                "popup": popup_html,
                "color": _severity_color(card),
                "dtpv": r["dtpv"] or "",
                "severity": severity,
                "date_sort": date_sort,
                "pog": int(r["pog"] or 0),
                "ran": int(r["ran"] or 0),
            },
            "geometry": {
                "type": "Point",
                "coordinates": [lon, lat],
            },
        })

    return {
        "type": "FeatureCollection",
        "features": features,
    }


# ============================================================
# Ленивая генерация Excel для существующей задачи
# ============================================================
@router.post("/tasks/{task_id}/generate-excel")
async def generate_excel_for_task(
    task_id: str,
    body: GenerateExcelRequest,
    user: TelegramUser = Depends(get_current_user),
):
    """
    Ленивая генерация Excel-файла для существующей задачи.

    Генерирует ОБА Excel-файла (карточки + участники) в thread pool.
    Первый запрос занимает 5-8 сек, результаты кэшируются на диске —
    повторный запрос для другого file_type отдаётся мгновенно.

    file_type: 'dtp_cards' — карточки ДТП, 'dtp_participants' — участники.
    """
    if body.file_type not in ("dtp_cards", "dtp_participants"):
        raise HTTPException(
            status_code=400,
            detail="file_type должен быть 'dtp_cards' или 'dtp_participants'",
        )

    task = await get_task_async(task_id)
    if not task or task.user_id != user.id:
        raise HTTPException(status_code=404, detail="Task not found")

    # Проверяем, есть ли уже файл на диске
    existing = next(
        (f for f in task.files if f["type"] == body.file_type),
        None,
    )
    if existing:
        path = Path(existing["path"])
        if path.exists():
            return FileResponse(
                path=str(path),
                media_type=existing["mime"],
                filename=existing["filename"],
            )

    # Восстанавливаем карточки если нужно
    if not task.cards:
        from ..services.pipeline import ensure_cards
        result = await ensure_cards(task)
        if not result.get("ok"):
            raise HTTPException(
                status_code=503,
                detail=result.get("error", "Карточки не загружены"),
            )

    # Генерируем ОБА файла (парсинг + Excel) в thread pool
    try:
        from ..services import _imports
        from ..services.pipeline import _task_dir

        gibdd_parser = _imports._import_module("gibdd_parser")
        excel_gen = _imports._import_module("excel_generator")

        # 1. Парсинг (CPU-bound)
        file1_data, file2_data = await asyncio.to_thread(
            _build_excel_data_sync, gibdd_parser, task.cards
        )

        # 2. Генерация Excel (I/O-bound + CPU-bound)
        file1_bytes, file2_bytes = await asyncio.to_thread(
            excel_gen.generate_both_files, file1_data, file2_data
        )

        # 3. Сохраняем ОБА файла на диск (кэш для повторных запросов)
        out_dir = _task_dir(task_id)
        region_safe = "".join(
            c if c.isascii() and c.isalnum() else "_" for c in task.region_name
        )[:30] or task.region_code
        period_safe = "".join(
            c if c.isascii() and c.isalnum() else "_" for c in task.period_label
        )[:20]

        cards_path = out_dir / f"dtp_cards_{region_safe}_{period_safe}.xlsx"
        cards_path.write_bytes(file1_bytes)

        # Регистрируем в task.files (если ещё нет)
        if not any(f["type"] == "dtp_cards" for f in task.files):
            task.files.append({
                "type": "dtp_cards",
                "filename": cards_path.name,
                "path": str(cards_path),
                "size_bytes": len(file1_bytes),
                "mime": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            })

        uch_path = out_dir / f"dtp_uch_{region_safe}_{period_safe}.xlsx"
        uch_path.write_bytes(file2_bytes)

        if not any(f["type"] == "dtp_participants" for f in task.files):
            task.files.append({
                "type": "dtp_participants",
                "filename": uch_path.name,
                "path": str(uch_path),
                "size_bytes": len(file2_bytes),
                "mime": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            })

        # 4. Отдаём запрошенный файл
        if body.file_type == "dtp_cards":
            return FileResponse(
                path=str(cards_path),
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                filename=cards_path.name,
            )
        else:
            return FileResponse(
                path=str(uch_path),
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                filename=uch_path.name,
            )

    except Exception as exc:
        logger.exception(f"generate_excel_for_task({task_id}) failed")
        raise HTTPException(
            status_code=500,
            detail=f"Ошибка генерации Excel: {exc}",
        )


# ============================================================
# Выгрузка файлов без аналитики (отдельная вкладка)
# ============================================================
@router.post("/export-only")
async def export_only(
    body: ExportOnlyRequest,
    user: TelegramUser = Depends(get_current_user),
):
    """
    Выгрузка Excel-файлов без аналитики, карты и очагов.

    Используется отдельной вкладкой «Выгрузка файлов».
    Только: запрос карточек → генерация Excel → отдача ZIP.

    Возвращает ZIP-архив с двумя файлами:
    - dtp_cards_{регион}_{период}.xlsx
    - dtp_uch_{регион}_{период}.xlsx
    """
    # 1. Запрашиваем карточки
    from ..services import _imports

    bot_module = _imports._import_module("bot")
    cards, errors = await bot_module._fetch_cards_for_period(
        dat_list=body.dat_list,
        reg_code=body.region_code,
        log_prefix=f"ExportOnly[user={user.id}]",
        cache_result=True,
    )

    if not cards:
        raise HTTPException(
            status_code=404,
            detail=(
                "Не удалось получить данные ДТП. "
                f"Ошибки: {'; '.join(errors[:3]) if errors else 'нет данных'}"
            ),
        )

    # 2. Генерируем Excel
    try:
        gibdd_parser = _imports._import_module("gibdd_parser")
        file1_data, file2_data = await asyncio.to_thread(
            _build_excel_data_sync, gibdd_parser, cards
        )

        excel_gen = _imports._import_module("excel_generator")
        file1_bytes, file2_bytes = await asyncio.to_thread(
            excel_gen.generate_both_files, file1_data, file2_data
        )
    except Exception as exc:
        logger.exception(f"export_only failed for {body.region_code}")
        raise HTTPException(
            status_code=500,
            detail=f"Ошибка генерации Excel: {exc}",
        )

    # 3. Упаковываем в ZIP
    import zipfile

    # isalnum() пропускает кириллицу, а HTTP-заголовки — latin-1.
    safe_region = "".join(
        c if c.isascii() and c.isalnum() else "_" for c in body.region_name
    )[:30] or body.region_code
    safe_period = "".join(
        c if c.isascii() and c.isalnum() else "_" for c in body.period_label
    )[:20]

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            f"dtp_cards_{safe_region}_{safe_period}.xlsx",
            file1_bytes,
        )
        zf.writestr(
            f"dtp_uch_{safe_region}_{safe_period}.xlsx",
            file2_bytes,
        )

    zip_bytes = zip_buf.getvalue()
    zip_name = f"dtp_{safe_region}_{safe_period}.zip"
    safe_zip_name = "".join(
        c if (c.isascii() and c.isalnum()) or c in "._-" else "_"
        for c in zip_name
    )[:100]

    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{safe_zip_name}"; '
                f"filename*=UTF-8''{safe_zip_name}"
            )
        },
    )


# ============================================================
# Скачивание ранее сгенерированных файлов (legacy, для кластеров/точки)
# ============================================================
@router.get("/tasks/{task_id}/files", response_model=List[TaskFileSchema])
async def list_task_files(
    task_id: str,
    user: TelegramUser = Depends(get_current_user),
):
    """Возвращает список файлов, сгенерированных задачей."""
    task = await get_task_async(task_id)
    if not task or task.user_id != user.id:
        raise HTTPException(status_code=404, detail="Task not found")
    return [TaskFileSchema(**f) for f in task.files]


@router.get("/tasks/{task_id}/download/{file_type}")
async def download_file(
    task_id: str,
    file_type: str,
    user: TelegramUser = Depends(get_current_user),
):
    """
    Скачивание ранее сгенерированного файла (кластеры, точка и т.д.).
    Для Excel-выгрузки ДТП используйте POST /tasks/{id}/generate-excel.
    """
    task = await get_task_async(task_id)
    if not task or task.user_id != user.id:
        raise HTTPException(status_code=404, detail="Task not found")

    file_meta = next(
        (f for f in task.files if f["type"] == file_type),
        None,
    )
    if not file_meta:
        raise HTTPException(
            status_code=404,
            detail=f"File of type '{file_type}' not found",
        )

    path = Path(file_meta["path"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="File missing on disk")

    return FileResponse(
        path=str(path),
        media_type=file_meta["mime"],
        filename=file_meta["filename"],
    )


# ============================================================
# Helpers
# ============================================================
def _build_excel_data_sync(gibdd_parser, cards):
    """Синхронный хелпер: build_file1_data + build_file2_data."""
    file1_data = gibdd_parser.build_file1_data(cards)
    file2_data = gibdd_parser.build_file2_data(cards)
    return file1_data, file2_data


async def _fetch_initial_pap_data(task: Task) -> list[dict]:
    """Извлекает ПАП-данные для начальной загрузки карты.

    Использует task.dat_list (периоды задачи), а не карточки,
    потому что cards из архива не содержат поле dat_period.
    Делегирует запрос pap_repository.fetch_pap_for_map(),
    который читает из таблицы pap_points.
    """
    from ..db.pap_repository import fetch_pap_for_map

    dat_periods = getattr(task, 'dat_list', None) or []
    if not dat_periods:
        return []

    return await fetch_pap_for_map(task.region_code, dat_periods)


async def _generate_map_html(task: Task) -> Optional[str]:
    """Генерирует HTML-карту и кэширует в task.files."""
    from ..services import _imports

    try:
        report_gen_module = _imports._import_module("report_generator")

        # Подгружаем камеры
        cameras = None
        try:
            camera_cache_module = _imports._import_module("camera_cache")
            if camera_cache_module.has_cached_cameras(task.region_code):
                cameras = camera_cache_module.load_cameras_from_cache(
                    task.region_code
                )
        except Exception:
            cameras = None

        # Подгружаем ПАП-данные из таблицы pap_points (если доступна)
        pap_data = None
        try:
            pap_data = await _fetch_initial_pap_data(task)
        except Exception:
            logger.warning(f"Task {task.id}: PAP data fetch failed, skipping")

        generator = report_gen_module.ReportGenerator(
            region_name=task.region_name,
            period_label=task.period_label,
        )

        prev_cards_for_map = task.prev_cards or None
        html_content = generator.generate_dtp_map(
            task.cards,
            cameras=cameras,
            prev_cards=prev_cards_for_map,
            prev_label=task.prev_label,
            pap_data=pap_data,
            task_id=task.id,
        )

        # Кэшируем на диск
        from ..services.pipeline import _task_dir
        out_dir = _task_dir(task.id)
        region_safe = "".join(
            c if c.isascii() and c.isalnum() else "_" for c in task.region_name
        )[:30] or task.region_code
        period_safe = "".join(
            c if c.isascii() and c.isalnum() else "_" for c in task.period_label
        )[:20]
        map_path = out_dir / f"dtp_map_{region_safe}_{period_safe}.html"
        map_path.write_text(html_content, encoding="utf-8")

        task.files.append({
            "type": "map_html",
            "filename": map_path.name,
            "path": str(map_path),
            "size_bytes": len(html_content.encode("utf-8")),
            "mime": "text/html",
        })

        logger.info(
            f"Task {task.id}: map generated lazily — "
            f"{len(html_content.encode('utf-8')) // 1024} KB"
        )
        return html_content

    except Exception as exc:
        logger.warning(f"Task {task.id}: lazy map generation failed: {exc}")
        return None


def _task_to_response(task: Task) -> TaskStatusResponse:
    # N6: backpressure — считаем очередь перед semaphore.
    queue_pos = None
    queue_ahead = None
    if task.status in ("pending", "fetching"):
        try:
            from ..services.task_registry import _tasks
            from collections import OrderedDict
            if isinstance(_tasks, OrderedDict):
                waiting = [
                    t for t in _tasks.values()
                    if t.status in ("pending", "fetching")
                ]
                waiting.sort(key=lambda t: t.created_at)
                for idx, t in enumerate(waiting, 1):
                    if t.id == task.id:
                        queue_pos = idx
                        queue_ahead = idx - 1
                        break
        except Exception:
            pass

    return TaskStatusResponse(
        task_id=task.id,
        status=task.status,
        progress=task.progress,
        region_code=task.region_code,
        region_name=task.region_name,
        period=task.period_label,
        total_dtp=task.total_dtp,
        total_dead=task.total_dead,
        total_injured=task.total_injured,
        error=task.error,
        files=[TaskFileSchema(**f) for f in task.files],
        analytics=task.analytics,
        queue_position=queue_pos,
        queue_ahead=queue_ahead,
    )


@router.post("/tasks/{task_id}/compare")
async def compare_task_year(
    task_id: str,
    request: CompareYearRequest,
    user: TelegramUser = Depends(get_current_user),
):
    """Пересчитывает сравнение аналитики с указанным годом.

    SQL-агрегация за compare_year через calculate_metrics_from_db() —
    быстрые индексированные запросы к gibdd_cards + gibdd_participants.
    Python calculate_metrics() используется только для текущего периода
    (у которого карточки уже в памяти).

    compare_year=None → АППГ (year-1, по умолчанию).
    """
    from ..services.gibdd_service import get_task_async
    from ..services.analytics_ops import ensure_comparison
    from ..services.gibdd_service import ensure_cards

    task = await get_task_async(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    if task.user_id != user.id:
        raise HTTPException(status_code=403, detail="Нет доступа")

    # Sprint 3.1: восстанавливаем task.cards из cards_cache, если
    # задача была выгружена из in-memory LRU или после рестарта.
    if not task.cards:
        result = await ensure_cards(task)
        if not result.get("ok"):
            raise HTTPException(
                status_code=400,
                detail=result.get("error", "Карточки текущего периода не загружены"),
            )
    if not task.cards:
        raise HTTPException(
            status_code=400,
            detail="Карточки текущего периода не загружены",
        )

    # Вычисляем prev_dat_list: те же месяцы, другой год
    compare_year = request.compare_year
    try:
        first_dat = task.dat_list[0]
        _, current_year_str = first_dat.split(".")
        current_year = int(current_year_str)
    except (IndexError, ValueError):
        raise HTTPException(
            status_code=400,
            detail="Не удалось определить год из dat_list",
        )

    target_year = compare_year if compare_year else current_year - 1
    prev_dat_list = []
    for dat in task.dat_list:
        try:
            m, _ = dat.split(".")
            prev_dat_list.append(f"{m}.{target_year}")
        except Exception:
            continue

    if not prev_dat_list:
        raise HTTPException(
            status_code=400,
            detail="Не удалось вычислить период для сравнения",
        )

    # Формируем prev_label
    prev_label = task.period_label.replace(str(current_year), str(target_year))

    # Текущие метрики — из кэша или пересчитываем
    analytics_module = __import__("analytics", fromlist=["calculate_metrics"])
    cards_id = id(task.cards)
    if request.force_refresh or task.current_metrics is None or task.current_metrics_cards_id != cards_id:
        current_metrics = analytics_module.calculate_metrics(task.cards)
        task.current_metrics = current_metrics
        task.current_metrics_cards_id = cards_id
        # Сбрасываем и другие кэши, зависящие от cards
        task.cross_tables = None
        task.cross_tables_cards_id = None
        task.comparison = None
        logger.info(f"Task {task.id}: in-memory cache flushed (force_refresh)")
    else:
        current_metrics = task.current_metrics

    # Метрики за compare_year — SQL-агрегация (быстро, без загрузки карточек в RAM)
    from ..db.metrics import calculate_metrics_from_db

    prev_metrics = await calculate_metrics_from_db(task.region_code, prev_dat_list)
    logger.info(
        f"Task {task.id}: compare year={target_year} — "
        f"SQL metrics, total={prev_metrics.get('total') if prev_metrics else 'N/A'}, "
        f"alcohol={prev_metrics.get('alcohol', '?') if prev_metrics else 'N/A'}, "
        f"pedestrians={prev_metrics.get('pedestrians', '?') if prev_metrics else 'N/A'}"
    )

    if not prev_metrics:
        # Нет данных за этот год
        return {
            "ok": True,
            "has_prev_data": False,
            "prev_label": prev_label,
            "current_label": task.period_label,
            "comparison": None,
        }

    comparison = analytics_module.compare_metrics(current_metrics, prev_metrics)

    return {
        "ok": True,
        "has_prev_data": True,
        "prev_label": prev_label,
        "current_label": task.period_label,
        "comparison": comparison,
        "previous": prev_metrics,
        "current": current_metrics,
    }


@router.post("/tasks/{task_id}/multi-year")
async def multi_year_dynamics(
    task_id: str,
    user: TelegramUser = Depends(get_current_user),
):
    """Динамика по годам — параллельная SQL-агрегация за 4 года.

    Возвращает массив {year, total, deaths, injured} за текущий + до 4
    предыдущих лет. Используется для графика «Динамика по годам».
    """
    from ..services.gibdd_service import get_task_async
    from ..db.metrics import calculate_metrics_from_db

    task = await get_task_async(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    if task.user_id != user.id:
        raise HTTPException(status_code=403, detail="Нет доступа")

    # Определяем текущий год
    try:
        first_dat = task.dat_list[0]
        _, current_year_str = first_dat.split(".")
        current_year = int(current_year_str)
    except (IndexError, ValueError):
        raise HTTPException(
            status_code=400,
            detail="Не удалось определить год из dat_list",
        )

    # Текущие метрики (уже в памяти или пересчитываем)
    analytics_module = __import__("analytics", fromlist=["calculate_metrics"])
    cards_id = id(task.cards) if task.cards else None
    if task.cards and (task.current_metrics is None or task.current_metrics_cards_id != cards_id):
        current_metrics = analytics_module.calculate_metrics(task.cards)
        task.current_metrics = current_metrics
        task.current_metrics_cards_id = cards_id
    elif task.current_metrics is not None:
        current_metrics = task.current_metrics
    else:
        current_metrics = await calculate_metrics_from_db(task.region_code, task.dat_list)

    # Собираем годы: текущий + до 4 предыдущих
    years = []
    for y in range(current_year, max(current_year - 4, 2020), -1):
        if y == current_year:
            years.append((y, current_metrics))
        else:
            prev_dat_list = []
            for dat in task.dat_list:
                try:
                    m, _ = dat.split(".")
                    prev_dat_list.append(f"{m}.{y}")
                except Exception:
                    continue
            if prev_dat_list:
                years.append((y, prev_dat_list))

    # Параллельно считаем SQL для предыдущих годов
    async def _calc_year(year: int, dat_list_or_metrics):
        if isinstance(dat_list_or_metrics, dict):
            # Текущий год — метрики уже есть
            m = dat_list_or_metrics
        else:
            m = await calculate_metrics_from_db(task.region_code, dat_list_or_metrics)
        if m is None:
            return None
        return {
            "year": year,
            "total": m.get("total", 0),
            "deaths": m.get("deaths", 0),
            "injured": m.get("injured", 0),
        }

    results = await asyncio.gather(
        *[_calc_year(y, dl) for y, dl in years],
        return_exceptions=True,
    )

    data = []
    for r in results:
        if isinstance(r, BaseException):
            logger.warning(f"multi-year: query failed: {r}")
            continue
        if r is not None:
            data.append(r)

    # Сортируем по году (возрастание)
    data.sort(key=lambda x: x["year"])

    logger.info(
        f"Task {task.id}: multi-year dynamics — {len(data)} years, "
        f"range {data[0]['year'] if data else '?'}-{data[-1]['year'] if data else '?'}"
    )

    return {"ok": True, "years": data}
