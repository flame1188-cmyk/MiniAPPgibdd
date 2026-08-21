"""
core/analytics_core.py — синхронный расчёт аналитики (Фаза C.2).

Единственная публичная функция:
- build_analytics_sync(cards, prev_cards, prev_label) → analytics_dict

Назначение:
  analytics.build_full_analytics — CPU-bound расчёт метрик, cross-tables,
  comparison vs прошлый год. Уже синхронный.

  В core/ предоставляем как direct sync-вызов для Celery worker.

Возвращает:
  analytics_dict: dict[str, Any] — большой dict с метриками, cross_tables,
                  comparison, has_prev_data, current_label.
                  См. analytics.build_full_analytics для структуры.

Исключения:
  Пробрасывает исключения от analytics.build_full_analytics.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..services._imports import _import_module

logger = logging.getLogger(__name__)


def build_analytics_sync(
    cards: List[Dict[str, Any]],
    prev_cards: Optional[List[Dict[str, Any]]] = None,
    prev_label: Optional[str] = None,
) -> Dict[str, Any]:
    """Синхронно рассчитывает расширенный аналитический блок.

    Args:
        cards: Карточки ДТП текущего периода.
        prev_cards: Карточки ДТП прошлого периода (для comparison АППГ).
                    Если None — comparison не рассчитывается.
        prev_label: Человекочитаемая метка прошлого периода
                    (например "2024 год").

    Returns:
        analytics_dict: dict со структурой:
            {
                "total_dtp": int,
                "total_dead": int,
                "total_injured": int,
                "has_prev_data": bool,
                "comparison": {...} | None,
                "cross_tables": {...},
                "metrics": {...},
                ...
            }

    Raises:
        RuntimeError: Если модуль analytics не найден.
        Любые исключения от analytics.build_full_analytics.

    Пример (Celery task):
        from miniapp.backend.core import build_analytics_sync

        analytics = build_analytics_sync(
            cards=current_cards,
            prev_cards=prev_cards,
            prev_label="2024 год",
        )
        # analytics["total_dtp"], analytics["comparison"], ...
    """
    analytics_module = _import_module("analytics")

    analytics_dict = analytics_module.build_full_analytics(
        current_cards=cards,
        prev_cards=prev_cards if prev_cards else None,
        prev_label=prev_label,
    )

    logger.info(
        f"build_analytics_sync: {len(cards)} ДТП, "
        f"prev={'нет' if not prev_cards else f'{len(prev_cards)} ДТП'}, "
        f"keys={list(analytics_dict.keys())[:6]}..."
    )
    return analytics_dict
