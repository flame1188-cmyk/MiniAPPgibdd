"""
core/exporting.py — синхронная генерация Excel-байтов и HTML-карты (Фаза C.2).

Две публичные функции:
- generate_excel_bytes_sync(file1_data, file2_data) → (file1_bytes, file2_bytes)
- generate_map_html_sync(cards, region_name, period_label, cameras, prev_cards, prev_label) → html_str

Назначение:
  excel_generator.generate_both_files и report_generator.ReportGenerator.generate_dtp_map —
  CPU-bound операции (openpyxl + Leaflet HTML). Уже синхронные.

  В core/ предоставляем как direct sync-вызовы для Celery worker.

Возвращает:
  - generate_excel_bytes_sync: tuple[bytes, bytes] — готовые .xlsx файлы
  - generate_map_html_sync: str — самодостаточный HTML с inline JS

Исключения:
  Пробрасывает исключения от excel_generator / report_generator.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from ..services._imports import _import_module

logger = logging.getLogger(__name__)


def generate_excel_bytes_sync(
    file1_data: List[Dict[str, Any]],
    file2_data: List[Dict[str, Any]],
) -> Tuple[bytes, bytes]:
    """Синхронно генерирует оба Excel-файла как bytes.

    Args:
        file1_data: Данные для Файла 1 (одна строка = одно ДТП).
                    От build_excel_data_sync()[0].
        file2_data: Данные для Файла 2 (одна строка = один участник).
                    От build_excel_data_sync()[1].

    Returns:
        Кортеж (file1_bytes, file2_bytes):
        - file1_bytes: bytes — готовый .xlsx (dtp_cards)
        - file2_bytes: bytes — готовый .xlsx (dtp_participants)

    Raises:
        RuntimeError: Если модуль excel_generator не найден.
        Любые исключения от excel_generator.generate_both_files.

    Пример (Celery task):
        from miniapp.backend.core import build_excel_data_sync, generate_excel_bytes_sync

        file1_data, file2_data = build_excel_data_sync(cards)
        file1_bytes, file2_bytes = generate_excel_bytes_sync(file1_data, file2_data)
        # Дальше: записать на диск или загрузить в S3
    """
    excel_gen = _import_module("excel_generator")

    file1_bytes, file2_bytes = excel_gen.generate_both_files(
        file1_data, file2_data
    )

    total_kb = (len(file1_bytes) + len(file2_bytes)) // 1024
    logger.info(
        f"generate_excel_bytes_sync: file1={len(file1_data)} строк "
        f"({len(file1_bytes) // 1024} KB), "
        f"file2={len(file2_data)} строк ({len(file2_bytes) // 1024} KB), "
        f"total={total_kb} KB"
    )
    return file1_bytes, file2_bytes


def generate_map_html_sync(
    cards: List[Dict[str, Any]],
    region_name: str,
    period_label: str,
    cameras: Optional[List[Dict[str, Any]]] = None,
    prev_cards: Optional[List[Dict[str, Any]]] = None,
    prev_label: Optional[str] = None,
) -> str:
    """Синхронно генерирует HTML-карту с Leaflet и кластеризацией.

    Args:
        cards: Карточки ДТП текущего периода.
        region_name: Название региона (для заголовка карты).
        period_label: Метка периода (например "2025 год").
        cameras: Список камер ГИБДД (опционально). Если None — без камер.
        prev_cards: Карточки прошлого года (для динамики АППГ в сводке).
        prev_label: Метка прошлого периода.

    Returns:
        html_str: str — самодостаточный HTML с inline JS (Leaflet,
                  кластеризация, сводная таблица).

    Raises:
        RuntimeError: Если модуль report_generator не найден.
        Любые исключения от ReportGenerator.generate_dtp_map.

    Пример (Celery task):
        from miniapp.backend.core import generate_map_html_sync

        html = generate_map_html_sync(
            cards=cards,
            region_name="Республика Башкортостан",
            period_label="2025 год",
        )
        # Дальше: записать в /data/tasks/{task_id}/dtp_map_*.html
    """
    report_gen_module = _import_module("report_generator")

    generator = report_gen_module.ReportGenerator(
        region_name=region_name,
        period_label=period_label,
    )
    html_content = generator.generate_dtp_map(
        cards,
        cameras=cameras,
        prev_cards=prev_cards,
        prev_label=prev_label,
    )

    logger.info(
        f"generate_map_html_sync: {len(cards)} ДТП, "
        f"region={region_name}, period={period_label}, "
        f"html={len(html_content)} chars"
    )
    return html_content
