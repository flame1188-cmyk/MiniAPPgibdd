"""
core/clusters_core.py — синхронный расчёт очагов концентрации (Sprint 7, Фаза C.2).

Единственная публичная функция:
- calculate_clusters_sync(...) → dict (готовый результат для clusters_state.result)

Назначение:
  concentration_points.calculate_concentration_dynamics — async-функция
  (OSM Overpass + классификация + кластеризация + динамика vs прошлый год).

  В core/ предоставляем sync-обёртку для Celery worker. Принимает
  ПОДГОТОВЛЕННЫЕ данные (cards, prev_cards), НЕ Task.

Возвращает:
  dict со структурой, идентичной clusters_ops.start_clusters_calculation:
  {
      "total_clusters": int,
      "total_lost": int,
      "total_prev_matched": int,
      "total_preclusters": int,
      "current_total_dtp": int,
      "current_deaths": int,
      "current_injured": int,
      "dynamics": {...},
      "clusters": [...],         # сериализованные очаги
      "preclusters": [...],      # сериализованные предочаги
      "has_prev_data": bool,
      "prev_label": str | None,
      "current_label": str,
      "region_name": str,
      "raw_clusters_count": int, # количество raw очагов (с cards)
      "raw_preclusters_count": int,
  }

  raw_clusters и raw_preclusters (с полными cards внутри) НЕ включаются
  в результат — они слишком тяжёлые для JSON-сериализации в Redis.
  Celery-таск (C.4) при необходимости сохранит их отдельно (например, в
  PostgreSQL large object или S3-совместимое хранилище).

Исключения:
  Любые исключения от concentration_points пробрасываются наверх.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from ..services._imports import _import_module
# Переиспользуем существующий сериализатор из clusters_ops —
# он уже корректно обрабатывает все поля очагов.
from ..services.clusters_ops import _serialize_cluster

logger = logging.getLogger(__name__)


def calculate_clusters_sync(
    cards: List[Dict[str, Any]],
    prev_cards: Optional[List[Dict[str, Any]]] = None,
    prev_label: Optional[str] = None,
    reg_code: Optional[str] = None,
    region_name: str = "",
    current_label: str = "",
    cameras: Optional[List[Dict[str, Any]]] = None,
    log_prefix: str = "Celery[clusters]",
) -> Dict[str, Any]:
    """Синхронно рассчитывает очаги концентрации ДТП.

    Sync-обёртка над concentration_points.calculate_concentration_dynamics (async).

    Args:
        cards: Карточки ДТП текущего периода.
        prev_cards: Карточки ДТП прошлого периода (для динамики АППГ).
                    Если None или [] — динамика не рассчитывается.
        prev_label: Метка прошлого периода (например "2024 год").
        reg_code: Код региона (для OSM Overpass — фильтр по региону).
        region_name: Название региона (для результата).
        current_label: Метка текущего периода (для результата).
        cameras: Список камер ГИБДД (опционально, для enrichment предочагов).
        log_prefix: Префикс для логов.

    Returns:
        dict — см. модульный docstring. Структура идентична
        clusters_ops.start_clusters_calculation.state.result.

    Raises:
        RuntimeError: Если модуль concentration_points не найден или если
                      вызвана из running event loop.
        Любые исключения от concentration_points.calculate_concentration_dynamics.

    Пример (Celery task):
        from miniapp.backend.core import calculate_clusters_sync

        @app.task(queue="clusters")
        def clusters_task(cards, prev_cards, reg_code, ...):
            return calculate_clusters_sync(
                cards=cards,
                prev_cards=prev_cards,
                prev_label="2024 год",
                reg_code=reg_code,
                region_name="Республика Башкортостан",
                current_label="2025 год",
            )
    """
    cp_module = _import_module("concentration_points")

    async def _run() -> tuple:
        """Вызывает async-функцию и возвращает (clusters, preclusters_raw, polygons)."""
        return await cp_module.calculate_concentration_dynamics(
            current_cards=cards,
            prev_cards=prev_cards or [],
            progress_callback=None,
            settlement_polygons=None,
            reg_code=reg_code,
        )

    try:
        clusters, preclusters_raw, _polygons = asyncio.run(_run())
    except RuntimeError as exc:
        if "asyncio.run() cannot be called from a running event loop" in str(exc):
            raise RuntimeError(
                "calculate_clusters_sync() вызван из running event loop. "
                "Используйте concentration_points.calculate_concentration_dynamics "
                "напрямую (await) или вызывайте из sync-контекста (Celery worker)."
            ) from exc
        raise

    # === Классификация очагов (без изменений относительно clusters_ops) ===
    clusters_data = [_serialize_cluster(c) for c in clusters]
    current_only = [
        c for c in clusters
        if not c.get("_is_lost", False) and not c.get("_is_prev_matched", False)
    ]
    lost_clusters = [c for c in clusters if c.get("_is_lost", False)]
    prev_matched_clusters = [c for c in clusters if c.get("_is_prev_matched", False)]

    # Динамика — агрегат по всем статусам
    dynamics_summary: Dict[str, int] = {
        "repeated_growing": 0,
        "repeated_shrinking": 0,
        "repeated_stable": 0,
        "repeated_merged": 0,
        "new": 0,
        "new_with_neighbor": 0,
        "prev_matched": 0,
        "lost": 0,
    }
    for c in clusters:
        d = c.get("dynamics") or {}
        status = d.get("status", "new")
        if status in dynamics_summary:
            dynamics_summary[status] += 1
        else:
            dynamics_summary[status] = 1

    # Предочаги
    preclusters_raw = preclusters_raw or []
    preclusters = [_serialize_cluster(p) for p in preclusters_raw]

    result: Dict[str, Any] = {
        "total_clusters": len(current_only),
        "total_lost": len(lost_clusters),
        "total_prev_matched": len(prev_matched_clusters),
        "total_preclusters": len(preclusters),
        "current_total_dtp": sum(
            c.get("total_accidents", 0) for c in current_only
        ),
        "current_deaths": sum(c.get("deaths", 0) for c in current_only),
        "current_injured": sum(c.get("injured", 0) for c in current_only),
        "dynamics": dynamics_summary,
        "clusters": clusters_data,
        "preclusters": preclusters,
        "has_prev_data": bool(prev_cards),
        "prev_label": prev_label if prev_cards else None,
        "current_label": current_label,
        "region_name": region_name,
        # Метаданные для Celery-таска (C.3): нужны ли raw_clusters?
        "raw_clusters_count": len(clusters),
        "raw_preclusters_count": len(preclusters_raw),
    }

    logger.info(
        f"{log_prefix}: clusters calculated — "
        f"current={len(current_only)}, lost={len(lost_clusters)}, "
        f"prev_matched={len(prev_matched_clusters)}, "
        f"preclusters={len(preclusters)}"
    )
    return result
