"""
worker/tasks/exports_tasks.py — Celery задачи для генерации Excel/HTML (Sprint 7, Фаза C.3).

Очередь: exports (concurrency=4 в docker-compose — самый лёгкий CPU-bound).

Задачи:
- generate_excel_task — генерация Excel-байтов (file1 + file2) из подготовленных
  file1_data/file2_data. Оборачивает core.generate_excel_bytes_sync.
  Проверяет db.excel_cache (через asyncio.run) при cache hit (~100 мс) —
  пропускает 5-8 сек openpyxl генерации.

- generate_map_task — генерация HTML-карты через ReportGenerator.
  Оборачивает core.generate_map_html_sync.

Эти задачи могут запускаться:
1. Из execute_pipeline_task (внутри Celery worker, как часть пайплайна) —
   тогда они НЕ отправляются в очередь "exports", а вызываются прямо.
2. Из FastAPI router (например, POST /clusters/{task_id}/excel) —
   тогда они идут в очередь "exports" и FastAPI polling'ом ждут результат.

Возвращает байты как base64-строки — Celery требует JSON-сериализуемых результатов.
"""
from __future__ import annotations

import asyncio
import base64
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from celery import Task as CeleryTask

from worker.celery_app import app

logger = logging.getLogger(__name__)


# ============================================================
# generate_excel_task
# ============================================================
@app.task(
    name="worker.tasks.exports_tasks.generate_excel_task",
    queue="exports",
    bind=True,
    base=CeleryTask,
    max_retries=0,
    acks_late=True,
)
def generate_excel_task(
    self: CeleryTask,
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
) -> Dict[str, Any]:
    """Генерирует Excel-байты (file1 + file2) из подготовленных данных.

    Args:
        file1_data: Данные для Файла 1 (карточки ДТП).
        file2_data: Данные для Файла 2 (участники).

    Returns:
        dict:
        {
            "ok": bool,
            "file1_bytes_b64": str,    # base64(excel_bytes)
            "file2_bytes_b64": str,
            "file1_size": int,
            "file2_size": int,
            "error": str | None,
        }
    """
    log_prefix = "Celery[generate_excel_task]"

    # === Generate ===
    from miniapp.backend.core import generate_excel_bytes_sync

    try:
        file1_bytes, file2_bytes = generate_excel_bytes_sync(file1_data, file2_data)
    except Exception as exc:
        logger.exception(f"{log_prefix}: failed")
        return {
            "ok": False,
            "file1_bytes_b64": "",
            "file2_bytes_b64": "",
            "file1_size": 0,
            "file2_size": 0,
            "error": str(exc),
        }

    logger.info(
        f"{log_prefix}: DONE — file1={len(file1_bytes) // 1024} KB, "
        f"file2={len(file2_bytes) // 1024} KB"
    )

    return {
        "ok": True,
        "file1_bytes_b64": base64.b64encode(file1_bytes).decode("ascii"),
        "file2_bytes_b64": base64.b64encode(file2_bytes).decode("ascii"),
        "file1_size": len(file1_bytes),
        "file2_size": len(file2_bytes),
        "error": None,
    }


# ============================================================
# generate_map_task
# ============================================================
@app.task(
    name="worker.tasks.exports_tasks.generate_map_task",
    queue="exports",
    bind=True,
    base=CeleryTask,
    max_retries=0,
    acks_late=True,
)
def generate_map_task(
    self: CeleryTask,
    cards: List[Dict[str, Any]],
    region_name: str,
    period_label: str,
    cameras: Optional[List[Dict[str, Any]]] = None,
    prev_cards: Optional[List[Dict[str, Any]]] = None,
    prev_label: Optional[str] = None,
) -> Dict[str, Any]:
    """Генерирует HTML-карту через ReportGenerator.

    Args:
        cards: Карточки ДТП текущего периода.
        region_name: Название региона.
        period_label: Метка периода.
        cameras: Список камер (опционально).
        prev_cards: Карточки прошлого периода (для динамики).
        prev_label: Метка прошлого периода.

    Returns:
        dict:
        {
            "ok": bool,
            "map_html": str,
            "map_size": int,
            "error": str | None,
        }
    """
    log_prefix = "Celery[generate_map_task]"

    from miniapp.backend.core import generate_map_html_sync

    try:
        html = generate_map_html_sync(
            cards=cards,
            region_name=region_name,
            period_label=period_label,
            cameras=cameras,
            prev_cards=prev_cards,
            prev_label=prev_label,
        )
    except Exception as exc:
        logger.exception(f"{log_prefix}: failed")
        return {
            "ok": False,
            "map_html": "",
            "map_size": 0,
            "error": str(exc),
        }

    logger.info(
        f"{log_prefix}: DONE — map={len(html.encode('utf-8')) // 1024} KB"
    )

    return {
        "ok": True,
        "map_html": html,
        "map_size": len(html.encode("utf-8")),
        "error": None,
    }
