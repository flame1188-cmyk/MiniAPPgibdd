"""
Основной роутер: создание задач выгрузки, опрос статуса, скачивание файлов.

Архитектура endpoint'ов:
- POST /api/dtp/tasks           — создать задачу (async выполнение в фоне)
- GET  /api/dtp/tasks           — список задач пользователя
- GET  /api/dtp/tasks/{id}      — статус задачи (для polling)
- GET  /api/dtp/tasks/{id}/files— список готовых файлов
- GET  /api/dtp/tasks/{id}/map  — HTML-карта (для iframe)
- GET  /api/dtp/tasks/{id}/download/{file_type} — скачать Excel/HTML
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse, HTMLResponse
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

    task = create_task(
        user_id=user.id,
        region_code=region_code,
        region_name=region_name,
        period_label=period_label,
        dat_list=dat_list,
        raw_query=raw_query,
    )

    # Hotfix Sprint 7: гарантированно persist'им метаданные задачи в БД
    # ДО запуска execute_task. Раньше create_task использовал fire-and-
    # forget через asyncio.create_task(save_task(task)), что могло потерять
    # задачу при рестарте контейнера до выполнения корутины. Теперь: даже
    # если execute_task упадёт в самом начале (до первого _persist()), task
    # уже есть в БД со статусом PENDING — пользователь увидит его в списке
    # и сможет удалить/пересоздать.
    try:
        from ..db.repository import save_task
        await save_task(task)
    except Exception as exc:
        # Не роняем endpoint — задача уже в in-memory _tasks и доступна.
        # Но логируем на WARNING, чтобы было видно в мониторинге.
        import logging
        logging.getLogger(__name__).warning(
            f"create_dtp_task: persist task_id={task.id} failed: {exc} "
            f"(задача доступна in-memory, но может быть потеряна при рестарте)"
        )

    # Аудит обращения к ПДн (152-ФЗ): пользователь создал задачу выгрузки.
    # Логируем регион/период — этого достаточно для журнала доступа.
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
        pass  # аудит не должен ронять создание задачи

    # Асинхронный запуск в фоне — execute_task сам обновляет статус
    asyncio_create_task(task.id)

    return TaskCreateResponse(
        task_id=task.id,
        status=task.status,
        region_code=task.region_code,
        region_name=task.region_name,
        period=task.period_label,
    )


def asyncio_create_task(task_id: str) -> None:
    """
    Запускает pipeline через dispatcher.

    Dispatcher (worker.dispatcher) сам решает, куда отправить задачу:
    - USE_CELERY=true И REDIS_URL задан → Celery queue "gibdd"
      (execute_pipeline_task в worker-процессе)
    - иначе → asyncio.create_task(execute_task(task_id)) в текущем event loop
      (legacy in-memory path, Sprint 6 behavior)

    В Celery-режиме:
      - .delay() синхронный — отправляет в broker и возвращается
      - FastAPI сразу отдаёт HTTP 200, фронтенд polling'ом читает статус
        из Redis (worker.task_state) через GET /tasks/{id}
    В in-memory:
      - _schedule_async(execute_task(task_id)) — fire-and-forget
    """
    # Достаём Task для извлечения параметров (dat_list, reg_code, ...)
    from ..services.gibdd_service import get_task_async
    from worker.dispatcher import dispatch_execute_pipeline
    import asyncio

    async def _dispatch():
        task = await get_task_async(task_id)
        if task is None:
            # Task уже удалён из памяти (LRU eviction или restart) —
            # fallback на старый in-memory path, который сам загрузит метаданные
            # из БД через pipeline.execute_task.
            from ..services.gibdd_service import execute_task
            await execute_task(task_id)
            return

        dispatch_execute_pipeline(
            task_id=task.id,
            dat_list=task.dat_list,
            reg_code=task.region_code,
            region_name=task.region_name,
            period_label=task.period_label,
            prev_dat_list=None,  # gibdd_tasks вычислит из dat_list (год - 1)
            prev_label=task.prev_label,  # может быть None — вычислится внутри
            user_id=task.user_id,
            raw_query=task.raw_query,
        )

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # Не в event loop (например, из теста в sync-контексте) — запускаем
        # dispatcher синхронно. dispatch_execute_pipeline — sync-функция.
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
    """Удаляет задачу пользователя.

    Удаляет из БД, in-memory кэша и файлы с диска (data/tasks/{task_id}/,
    а также все файлы, перечисленные в task.files[].path).

    Ownership-проверка: можно удалить только свою задачу. Если task_id
    не существует или принадлежит другому пользователю — возвращаем 404
    (не раскрываем, какая именно причина, чтобы не давать информации
    о существовании чужих задач).

    Действие логируется в access_log (требование 152-ФЗ).
    """
    from ..db.repository import delete_task

    # Pre-check через get_task_async — для логирования и проверки ownership
    # перед тем, как вызывать repository.delete_task. Это даёт более точные
    # ошибки (404 vs 403) в in-memory режиме, когда БД недоступна.
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
        # Маловероятная ситуация: задача была в памяти на pre-check,
        # но к моменту delete_task уже исчезла (race condition с cleanup-воркером).
        # Возвращаем 404 — фронтенд инвалидирует кэш и пользователь увидит
        # актуальный список.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task {task_id} not found",
        )

    # Логируем удаление в access_log (152-ФЗ аудит)
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
        pass  # логирование не должно рушить удаление

    return {"ok": True, "task_id": task_id, "deleted": True}


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


@router.get("/tasks/{task_id}/map", response_class=HTMLResponse)
async def get_task_map(
    task_id: str,
    user: TelegramUser = Depends(get_current_user),
):
    """
    Отдаёт HTML-карту (inline Leaflet с кластеризацией).
    Используется в <iframe> на frontend.
    """
    task = await get_task_async(task_id)
    if not task or task.user_id != user.id:
        raise HTTPException(status_code=404, detail="Task not found")

    map_file = next(
        (f for f in task.files if f["type"] == "map_html"),
        None,
    )
    if not map_file:
        raise HTTPException(status_code=404, detail="Map file not generated yet")

    path = Path(map_file["path"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="Map file missing on disk")

    return HTMLResponse(content=path.read_text(encoding="utf-8"))


@router.get("/tasks/{task_id}/download/{file_type}")
async def download_file(
    task_id: str,
    file_type: str,
    user: TelegramUser = Depends(get_current_user),
):
    """
    Скачивание Excel/HTML-файла.

    file_type: 'dtp_cards' | 'dtp_participants' | 'map_html'
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
def _task_to_response(task: Task) -> TaskStatusResponse:
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
    )
