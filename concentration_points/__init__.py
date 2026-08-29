"""
Модуль расчёта очагов концентрации ДТП (мест концентрации аварийности).

Расщеплён на подмодули:
  _constants        — константы (пороги, колонки, статусы)
  _card_accessors   — извлечение данных из карточек ДТП
  _osm_boundaries   — OSM/Overpass, кэш, парсинг полигонов, classify_cards
  _clustering_core  — алгоритмы кластеризации (НП + вне НП)
  _dynamics         — сопоставление очагов с АППГ
  _excel_output     — Excel-формatters, камеры, колонки
"""

import asyncio
import logging
from typing import Callable, Awaitable

from shapely.geometry import Polygon, MultiPolygon

from ._osm_boundaries import (
    fetch_settlement_boundaries, classify_cards, close_overpass_client,
    _save_region_cache, _load_region_cache, _region_cache_path,
    _compute_bbox_tiles, _dedup_elements,
    _parse_overpass_elements_with_ids,
    _memory_cache, _overpass_client,
)
from ._clustering_core import (
    find_settlement_concentration_points,
    find_nonsettlement_concentration_points,
    find_settlement_preclusters,
    find_nonsettlement_preclusters,
    _extract_assigned_indices,
)
from ._dynamics import calculate_concentration_dynamics
from ._excel_output import (
    build_concentration_excel_data, build_concentration_detail_data,
    build_precluster_excel_data,
    build_dynamics_excel_data, build_dynamics_detail_data,
    build_dynamics_summary,
    get_concentration_column_names, get_detail_column_names,
    get_precluster_column_names,
    get_dynamics_column_names, get_dynamics_detail_column_names,
    enrich_clusters_with_cameras,
)
from ._card_accessors import haversine_meters, _parse_coords, _is_off_road
from ._constants import (
    OVERPASS_URLS, PLACE_FILTER,
    CACHE_DIR, REGION_CACHE_DIR, REGION_CACHE_TTL_SECONDS,
)

logger = logging.getLogger(__name__)


async def calculate_concentration_points(
    cards: list[dict],
    progress_callback: Callable[[str], Awaitable[None]] | None = None,
    settlement_polygons: list[Polygon | MultiPolygon] | None = None,
    reg_code: str | None = None,
) -> tuple[list[dict], list[dict], list[Polygon | MultiPolygon] | None]:
    """
    Главная функция: расчёт всех очагов концентрации ДТП.

    Args:
        cards: Список сырых карточек ДТП
        progress_callback: async-функция для обновления статуса
        settlement_polygons: Если переданы — используются вместо запроса к OSM.
            Это позволяет переиспользовать полигоны между вызовами
            (например, при сравнении с прошлым годом).
        reg_code: Код региона ГИБДД (например, "1145" для Москвы).
            Используется для проверки регион-уровневого кэша
            (предкэшированные top-N регионы от precache_osm.py).
            Игнорируется, если settlement_polygons уже передан.

    Returns:
        (очаги, предочаги, settlement_polygons) — полигоны для переиспользования.
    """
    if not cards:
        return [], [], None

    # Шаг 1: Фильтр — только карточки с координатами
    #   и исключаем ДТП вне дороги (внутридворовые, автостоянки)
    cards_with_coords = [
        c for c in cards
        if _parse_coords(c) and not _is_off_road(c)
    ]
    no_coords = len(cards) - len(cards_with_coords)

    if no_coords > 0:
        logger.warning(f"{no_coords} карточек без координат или вне дороги пропущены")

    if not cards_with_coords:
        logger.warning("Нет карточек с координатами — расчёт невозможен")
        return [], [], None

    # Шаг 2: Границы НП
    # Если полигоны переданы снаружи — используем их (OSM не запрашиваем)
    if settlement_polygons is None:
        settlement_polygons = await fetch_settlement_boundaries(
            cards_with_coords, progress_callback, reg_code=reg_code,
        )
    else:
        logger.info(
            f"Границы НП переданы извне: {len(settlement_polygons)} полигонов "
            f"(OSM-запрос пропущен)"
        )

    if not settlement_polygons:
        logger.warning(
            "Не удалось получить границы НП из OSM. "
            "Все ДТП будут обработаны как вне НП."
        )

    # Шаг 3: Классификация
    if progress_callback:
        await progress_callback(
            f"Классификация ДТП...\n"
            f"Всего с координатами: {len(cards_with_coords)}"
        )

    # CPU-bound операции выполняются в thread pool, чтобы не блокировать
    # event loop FastAPI. Для 3000+ карточек классификация + кластеризация
    # могут занять несколько секунд синхронно.
    if settlement_polygons:
        settlement_cards, non_settlement_cards = await asyncio.to_thread(
            classify_cards, cards_with_coords, settlement_polygons,
        )
    else:
        # Fallback: все как вне НП
        settlement_cards = []
        non_settlement_cards = cards_with_coords

    # Шаг 4: Очаги в НП (CPU-bound: 3 прохода кластеризации)
    if progress_callback:
        await progress_callback(
            f"Поиск очагов в НП ({len(settlement_cards)} ДТП)..."
        )

    settlement_clusters = await asyncio.to_thread(
        find_settlement_concentration_points, settlement_cards,
    )

    # Шаг 5: Очаги вне НП (CPU-bound: скользящее окно 1 км)
    if progress_callback:
        await progress_callback(
            f"Поиск очагов вне НП ({len(non_settlement_cards)} ДТП)..."
        )

    non_settlement_clusters = await asyncio.to_thread(
        find_nonsettlement_concentration_points, non_settlement_cards,
    )

    # Объединяем: сначала НП, потом вне НП
    all_clusters = settlement_clusters + non_settlement_clusters

    logger.info(
        f"Итого очагов: {len(all_clusters)} "
        f"(НП: {len(settlement_clusters)}, "
        f"вне НП: {len(non_settlement_clusters)})"
    )

    # Шаг 6: Предочаги (CPU-bound: попарные расстояния)
    if progress_callback:
        await progress_callback(
            f"Поиск предочагов..."
        )

    settlement_assigned = _extract_assigned_indices(
        settlement_clusters, settlement_cards,
    )
    settlement_preclusters = await asyncio.to_thread(
        find_settlement_preclusters, settlement_cards, settlement_assigned,
    )

    non_settlement_assigned = _extract_assigned_indices(
        non_settlement_clusters, non_settlement_cards,
    )
    non_settlement_preclusters = await asyncio.to_thread(
        find_nonsettlement_preclusters, non_settlement_cards, non_settlement_assigned,
    )

    all_preclusters = settlement_preclusters + non_settlement_preclusters

    logger.info(
        f"Итого предочагов: {len(all_preclusters)} "
        f"(НП: {len(settlement_preclusters)}, "
        f"вне НП: {len(non_settlement_preclusters)})"
    )

    return all_clusters, all_preclusters, settlement_polygons


# ========================
# Историческая динамика очагов
# ========================

