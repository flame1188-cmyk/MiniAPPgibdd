"""
core/parsing.py — синхронный парсинг карточек ДТП в данные для Excel (Фаза C.2).

Единственная публичная функция:
- build_excel_data_sync(cards) → (file1_data, file2_data)

Назначение:
  gibdd_parser.build_file1_data + build_file2_data — CPU-bound парсинг
  1500-3000 карточек. Уже синхронный, но в pipeline.py вызывается через
  asyncio.to_thread(), чтобы не блокировать event loop.

  В core/ мы предоставляем ту же логику как direct sync-вызов —
  Celery worker сам по себе sync, to_thread не нужен.

Возвращает:
  (file1_data, file2_data):
  - file1_data: list[dict] — одна строка = одно ДТП (для Excel Файл 1)
  - file2_data: list[dict] — одна строка = один участник ДТП (для Excel Файл 2)

Исключения:
  Пробрасывает исключения от gibdd_parser (KeyError, TypeError при
  malformed cards). Celery-таск должен ловить и помечать как FAILED.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

from ..services._imports import _import_module

logger = logging.getLogger(__name__)


def build_excel_data_sync(
    cards: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Синхронно парсит карточки ДТП в данные для Excel Файл 1 и Файл 2.

    Args:
        cards: Список словарей карточек ДТП (от fetch_cards_for_period_sync).

    Returns:
        Кортеж (file1_data, file2_data):
        - file1_data: список словарей, одна строка = одно ДТП
        - file2_data: список словарей, одна строка = один участник ДТП

    Raises:
        RuntimeError: Если модуль gibdd_parser не найден.
        Любые исключения от gibdd_parser.build_file1/2_data.

    Пример (Celery task):
        from miniapp.backend.core import fetch_cards_for_period_sync, build_excel_data_sync

        cards, _ = fetch_cards_for_period_sync(["1.2025"], "1101")
        file1_data, file2_data = build_excel_data_sync(cards)
        # Дальше: generate_excel_bytes_sync(file1_data, file2_data)
    """
    gibdd_parser = _import_module("gibdd_parser")

    file1_data = gibdd_parser.build_file1_data(cards)
    file2_data = gibdd_parser.build_file2_data(cards)

    logger.info(
        f"build_excel_data_sync: {len(cards)} ДТП → "
        f"file1_data={len(file1_data)} строк, "
        f"file2_data={len(file2_data)} строк"
    )
    return file1_data, file2_data
