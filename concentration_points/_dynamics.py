"""Динамика очагов — сопоставление текущих с АППГ."""
import logging
from typing import Callable, Awaitable, Any

from shapely.geometry import Polygon, MultiPolygon

from ._constants import (
    MATCH_RADIUS_SETTLEMENT, MATCH_RADIUS_NONSETTLEMENT,
    REPEATED_RADIUS_M, NEIGHBOR_RADIUS_SETTLEMENT, NEIGHBOR_RADIUS_NONSETTLEMENT,
    MAX_NEIGHBORS_TO_SHOW, DYNAMICS_STATUS_LABELS,
    SETTLEMENT_INTERSECTION_RADIUS_M, SETTLEMENT_OTHER_RADIUS_M,
    SETTLEMENT_ROAD_WINDOW_KM, SETTLEMENT_ROAD_GPS_MAX_M,
    NON_SETTLEMENT_WINDOW_KM, NON_SETTLEMENT_NO_PK_WINDOW_KM,
    NON_SETTLEMENT_GPS_MAX_SPREAD_M,
    SAME_TYPE_THRESHOLD, ANY_TYPE_THRESHOLD,
    PRE_SAME_TYPE_THRESHOLD, PRE_ANY_TYPE_THRESHOLD,
    INTERSECTION_KEYWORDS, EXCLUDED_SDOR_ALWAYS, EXCLUDED_K_UL,
    EXCLUDED_SDOR_FOR_KUL, ZONE_TYPE_LABELS,
)
from ._card_accessors import (
    haversine_meters, _parse_coords, _is_intersection, _is_off_road,
    _get_dtp_type, _get_road_name, _get_date, _get_km_m, _has_road_and_piketazh,
    _max_gps_spread,
)
from ._osm_boundaries import (
    classify_cards, fetch_settlement_boundaries, close_overpass_client,
)
from ._clustering_core import (
    find_settlement_concentration_points, find_nonsettlement_concentration_points,
    find_settlement_preclusters, find_nonsettlement_preclusters,
    _build_cluster, _build_precluster,
    _check_cluster_criteria, _check_precluster_criteria,
    _cluster_cards_by_radius, _extract_assigned_indices,
    _matches_sop_filter, _build_cause_counters, _format_counter,
)
from analytics import _safe_int

logger = logging.getLogger(__name__)

def _piketazh_ranges_intersect(curr: dict, prev: dict) -> bool:
    """
    Проверяет пересечение диапазонов пикетажа двух очагов.

    Использует dtp_pk_min/max — реальные границы ДТП в очаге
    (не окно группировки start_pos/end_pos, которое шире).

    Возвращает True, если диапазоны пересекаются (хотя бы одна точка).
    Если у одного из очагов нет пикетажа — возвращает False
    (для таких случаев нужен другой критерий — см. _dtp_within_100m).
    """
    curr_min = curr.get("dtp_pk_min")
    curr_max = curr.get("dtp_pk_max")
    prev_min = prev.get("dtp_pk_min")
    prev_max = prev.get("dtp_pk_max")

    if None in (curr_min, curr_max, prev_min, prev_max):
        return False

    # Пересечение диапазонов: max(min1, min2) <= min(max1, max2)
    return max(curr_min, prev_min) <= min(curr_max, prev_max)


def _dtp_within_radius(
    curr: dict,
    prev: dict,
    radius_m: float,
) -> bool:
    """
    Проверяет, есть ли хотя бы одна пара ДТП (curr_card, prev_card)
    на расстоянии <= radius_m друг от друга.

    Используется для очагов без пикетажа (в НП пикетаж часто пустой) —
    это эквивалент пересечения границ, но в координатах.

    ВНИМАНИЕ: ранее тут была оптимизация «если center-to-center >
    2*radius + 500м — пропустить». Для вытянутых линейных очагов
    (участок трассы 5км) center может быть далеко от center соседа,
    при этом на концах участки подходят вплотную. Оптимизация убрана —
    для типичных размеров кластеров (3-15 ДТП) попарная проверка
    выполняется за <1мс.
    """
    curr_cards = curr.get("cards") or []
    prev_cards = prev.get("cards") or []
    if not curr_cards or not prev_cards:
        return False

    # Берём координаты из карточек через _parse_coords
    curr_coords = []
    for card in curr_cards:
        c = _parse_coords(card)
        if c:
            curr_coords.append(c)
    prev_coords = []
    for card in prev_cards:
        c = _parse_coords(card)
        if c:
            prev_coords.append(c)

    if not curr_coords or not prev_coords:
        return False

    for clat, clon in curr_coords:
        for plat, plon in prev_coords:
            if haversine_meters(clat, clon, plat, plon) <= radius_m:
                return True
    return False


def _min_dtp_distance(curr: dict, prev: dict) -> float:
    """
    Минимальное попарное расстояние между ДТП двух очагов (в метрах).

    Используется для сортировки соседей: чем меньше мин. расстояние,
    тем «ближе» прошлогодний очаг к текущему.

    Если у одного из очагов нет координат — возвращает большое число
    (чтобы такой сосед оказался в конце списка).
    """
    curr_cards = curr.get("cards") or []
    prev_cards = prev.get("cards") or []
    if not curr_cards or not prev_cards:
        return float("inf")

    curr_coords = [
        c for c in (_parse_coords(card) for card in curr_cards) if c
    ]
    prev_coords = [
        c for c in (_parse_coords(card) for card in prev_cards) if c
    ]
    if not curr_coords or not prev_coords:
        return float("inf")

    min_d = float("inf")
    for clat, clon in curr_coords:
        for plat, plon in prev_coords:
            d = haversine_meters(clat, clon, plat, plon)
            if d < min_d:
                min_d = d
    return min_d


def _roads_compatible(curr: dict, prev: dict) -> bool:
    """
    Проверяет совместимость названий дорог.

    Возвращает True, если дороги можно считать одной дорогой:
    - обе пустые (не указаны)
    - одна пустая (не блокируем)
    - обе указаны и совпадают (case-insensitive, после trim)

    Возвращает False, если обе указаны и различаются.
    """
    curr_road = curr["road"].strip().lower()
    prev_road = prev["road"].strip().lower()
    if curr_road and prev_road and curr_road != prev_road:
        return False
    return True


def _match_clusters(
    current_clusters: list[dict],
    prev_clusters: list[dict],
) -> dict[int, list[int]]:
    """
    Сопоставляет текущие очаги с прошлогодними по НОВОЙ методологии.

    Алгоритм:

    1. Проход «повторные очаги»:
       Для каждого текущего очага ищет ВСЕ прошлые, которые:
       - совместимы по названию дороги (см. _roads_compatible)
       - пересекаются по пикетажу (если есть пикетаж у обоих)
         ИЛИ имеют хотя бы одну пару ДТП в радиусе REPEATED_RADIUS_M
         (для очагов без пикетажа — типично для НП)

       Если найден 1 прошлый → «повторный» (с подстатусом по изменению count).
       Если найдено 2+ прошлых → «повторный (слияние)».

       Один прошлый очаг может быть сматчен с несколькими текущими
       (это не слияние — просто два текущих очага пересекают один прошлый).
       Но для определения «исчезнувший» важен факт: был ли хоть один
       текущий, который сматчился с этим прошлым.

    2. Проход «соседи» (для не-повторных):
       Для текущих очагов без матча ищет прошлые в радиусе
       NEIGHBOR_RADIUS_SETTLEMENT (250м для НП) или
       NEIGHBOR_RADIUS_NONSETTLEMENT (1000м для вне-НП) —
       БЕЗ проверки дороги.
       Если есть хотя бы один сосед → «новый (есть ближайший в АППГ)»
       с списком до MAX_NEIGHBORS_TO_SHOW ближайших.
       Иначе → «новый».

    Returns:
        {current_index: [prev_index, ...]}
        Пустой список [] для текущих, у которых нет повтора.
        Для «новых с соседом» соседи хранятся в поле neighbors
        (см. _annotate_clusters_with_matches).
    """
    # matches[ci] = list of prev indices (повторные)
    matches: dict[int, list[int]] = {ci: [] for ci in range(len(current_clusters))}

    # === Проход 1: повторные очаги ===
    for ci, curr in enumerate(current_clusters):
        curr_has_pk = (
            curr.get("dtp_pk_min") is not None
            and curr.get("dtp_pk_max") is not None
        )

        for pi, prev in enumerate(prev_clusters):
            # Дорога должна быть совместимой
            if not _roads_compatible(curr, prev):
                continue

            # Zone_type должен совпадать по префиксу (НП vs вне-НП)
            curr_in_settlement = curr["zone_type"].startswith("settlement")
            prev_in_settlement = prev["zone_type"].startswith("settlement")
            if curr_in_settlement != prev_in_settlement:
                continue

            # Проверка пересечения
            if curr_has_pk:
                # По пикетажу
                if _piketazh_ranges_intersect(curr, prev):
                    matches[ci].append(pi)
            else:
                # По ДТП в радиусе 100м
                if _dtp_within_radius(curr, prev, REPEATED_RADIUS_M):
                    matches[ci].append(pi)

    # Логирование прохода 1
    repeated_count = sum(1 for v in matches.values() if len(v) > 0)
    merged_count = sum(1 for v in matches.values() if len(v) >= 2)
    logger.info(
        f"Сопоставление очагов: {len(current_clusters)} текущих, "
        f"{len(prev_clusters)} прошлых, "
        f"повторных {repeated_count} (из них слияний {merged_count}), "
        f"новых {len(current_clusters) - repeated_count}"
    )

    # === Проход 2: соседи для не-повторных ===
    # ВАЖНО: соседи ищутся БЕЗ проверки zone_type и БЕЗ проверки дороги.
    # Логика: «новый (есть ближайший в АППГ)» — это про географическую
    # близость, а не про совпадение дороги/зоны. Например, текущий очаг
    # в НП может иметь ближайший прошлогодний очаг на прилегающей
    # трассе (вне НП) — это всё равно «есть сосед в АППГ».
    #
    # ВАЖНО: используем _dtp_within_radius (попарная проверка ДТП),
    # а не center-to-center. Для вытянутых линейных очагов (участок
    # трассы длиной 5 км) center может быть далеко от center соседа,
    # при этом на концах участки могут подходить вплотную.
    neighborless_curr: list[tuple[int, float]] = []  # (ci, min_dist_to_any_prev)
    for ci, curr in enumerate(current_clusters):
        if matches[ci]:  # уже повторный
            continue

        curr_in_settlement = curr["zone_type"].startswith("settlement")
        radius = (
            NEIGHBOR_RADIUS_SETTLEMENT
            if curr_in_settlement
            else NEIGHBOR_RADIUS_NONSETTLEMENT
        )

        # Ищем ВСЕ прошлые в радиусе (по ДТП-к-ДТП, не center-to-center)
        neighbors: list[tuple[int, float]] = []
        # Для диагностики — минимальное расстояние до любого prev
        min_dist_any: float | None = None
        for pi, prev in enumerate(prev_clusters):
            # Для диагностики считаем center-to-center (мин. по всем prev).
            # Раньше тут была отбраковка «cd > 2*radius + 500 → пропустить»,
            # но для вытянутых очагов center может быть далеко, а ДТП на
            # концах — близко. Отбраковка убрана, _dtp_within_radius
            # проверяет все пары и сама принимает решение.
            cc = curr.get("center")
            pc = prev.get("center")
            if cc and pc:
                cd = haversine_meters(cc[0], cc[1], pc[0], pc[1])
                if min_dist_any is None or cd < min_dist_any:
                    min_dist_any = cd

            # Точная проверка: есть ли пара ДТП в радиусе
            if _dtp_within_radius(curr, prev, radius):
                # Для соседа дистанция = минимальное попарное расстояние
                d = _min_dtp_distance(curr, prev)
                neighbors.append((pi, d))

        if neighbors:
            # Сортируем по расстоянию, берём до MAX_NEIGHBORS_TO_SHOW
            neighbors.sort(key=lambda x: x[1])
            matches[ci] = []  # повторных нет, но соседи есть
            # Сохраняем соседей в скрытом поле curr — позже используем
            # в _annotate_clusters_with_matches
            curr["_neighbors"] = [
                {"prev_index": pi, "distance_m": dist}
                for pi, dist in neighbors[:MAX_NEIGHBORS_TO_SHOW]
            ]
            logger.info(
                f"Сосед: текущий очаг #{ci} "
                f"(road='{curr['road']}' zone={curr['zone_type']}) "
                f"-> {len(neighbors)} соседей в АППГ, "
                f"ближайший на {neighbors[0][1]:.0f}м"
            )
        else:
            # Нет ни соседей — оставляем curr без _neighbors
            # (статус будет «новый»)
            neighborless_curr.append((ci, min_dist_any or 0.0))

    # Диагностика: если многие новые очаги не нашли соседей,
    # логируем минимальные расстояния — это поможет понять,
    # слишком ли мал радиус или другая причина.
    if neighborless_curr:
        # Топ-5 ближайших из «не нашедших соседей»
        neighborless_curr.sort(key=lambda x: x[1])
        top5 = neighborless_curr[:5]
        dists_str = ", ".join(
            f"#{ci}={d:.0f}м" for ci, d in top5
        )
        logger.info(
            f"Соседи не найдены для {len(neighborless_curr)} новых очагов. "
            f"Топ-5 ближайших (мин. dist до любого prev): {dists_str}"
        )

    return matches




async def calculate_concentration_dynamics(
    current_cards: list[dict],
    prev_cards: list[dict],
    progress_callback: Callable[[str], Awaitable[None]] | None = None,
    settlement_polygons: list[Polygon | MultiPolygon] | None = None,
    reg_code: str | None = None,
) -> tuple[list[dict], list[Polygon | MultiPolygon] | None, list[dict]]:
    """
    Рассчитывает очаги для двух периодов и определяет динамику каждого.

    Возвращает кортеж ``(clusters, settlement_polygons, preclusters)``.
    Третий элемент — список предочагов текущего периода. ВАЖНО: предочаги
    возвращаются отдельно, потому что они существуют даже когда очагов нет
    (например, в небольших регионах с малым числом ДТП). Ранее предочаги
    прикреплялись к ``clusters[0]["_preclusters"]`` — но при пустом списке
    очагов они терялись.

    Границы НП загружаются из OSM **один раз** по объединённому bbox
    обоих периодов — это сокращает нагрузку на Overpass API в 2 раза.
    Если передан settlement_polygons — используется без запроса к OSM.

    Каждому очагу добавляется ключ ``dynamics``:
    {
        "status": "new" | "lost" | "growing" | "shrinking" | "stable",
        "prev_total": int | None,       # ДТП в прошлом периоде
        "prev_deaths": int | None,      # погибло в прошлом периоде
        "prev_injured": int | None,     # ранено в прошлом периоде
        "match_distance": float | None, # расстояние до прошлого очага (м)
    }

    Порядок результата: текущие очаги (с аннотацией динамики),
    затем исчезнувшие очаги (из прошлого периода).

    Args:
        current_cards: Карточки ДТП текущего периода
        prev_cards: Карточки ДТП прошлого периода (те же месяцы)
        progress_callback: async-функция для обновления статуса
        settlement_polygons: Если переданы — используются вместо запроса к OSM.
        reg_code: Код региона ГИБДД для проверки регион-уровневого кэша.

    Returns:
        (очаги_с_dynamics, settlement_polygons) — полигоны для переиспользования.
    """
    # Ленивый импорт: calculate_concentration_points определена в __init__.py,
    # который импортирует текущий модуль → module-level импорт создаёт цикл.
    from . import calculate_concentration_points as _calc_cp

    # --- Готовим карточки с координатами из обоих периодов ---
    current_filtered = [
        c for c in current_cards
        if _parse_coords(c) and not _is_off_road(c)
    ]
    prev_filtered = [
        c for c in prev_cards
        if _parse_coords(c) and not _is_off_road(c)
    ]

    if not current_filtered:
        logger.warning("Нет карточек текущего периода с координатами")
        return [], None

    # --- Загружаем границы НП ОДИН РАЗ по объединённому bbox ---
    if settlement_polygons is None:
        combined_cards = current_filtered + prev_filtered
        if prev_filtered:
            if progress_callback:
                await progress_callback(
                    f"Загрузка границ НП из OpenStreetMap...\n"
                    f"(Один запрос для обоих периодов)\n"
                    f"ДТП текущего: {len(current_filtered)}, "
                    f"прошлого: {len(prev_filtered)}"
                )
            settlement_polygons = await fetch_settlement_boundaries(
                combined_cards, progress_callback, reg_code=reg_code,
            )
        else:
            settlement_polygons = await fetch_settlement_boundaries(
                current_filtered, progress_callback, reg_code=reg_code,
            )

        if settlement_polygons:
            logger.info(
                f"Динамика: границы НП загружены один раз: "
                f"{len(settlement_polygons)} полигонов "
                f"(OSM-запрос пропущен для прошлого периода)"
            )
    else:
        logger.info(
            f"Динамика: границы НП переданы извне: "
            f"{len(settlement_polygons)} полигонов (OSM-запрос пропущен)"
        )

    # --- Очаги текущего периода ---
    if progress_callback:
        await progress_callback("Расчёт очагов текущего периода...")
    current_clusters, current_preclusters, _polys = await _calc_cp(
        current_cards,
        progress_callback,
        settlement_polygons=settlement_polygons,
        reg_code=reg_code,
    )

    if not prev_cards:
        # Данных за прошлый год нет — все очаги помечаем как «новые»
        for c in current_clusters:
            c["dynamics"] = {
                "status": "new",
                "prev_total": None,
                "prev_deaths": None,
                "prev_injured": None,
                "match_distance": None,
            }
        logger.info(
            f"Динамика: нет данных за прошлый год, "
            f"{len(current_clusters)} очагов помечены как новые"
        )
        # Backward-compat: предочаги в clusters[0]["_preclusters"] (если очаги есть)
        if current_clusters:
            current_clusters[0]["_preclusters"] = current_preclusters
        # ВАЖНО: предочаги возвращаются отдельно — даже когда очагов нет
        return current_clusters, settlement_polygons, current_preclusters

    # --- Очаги прошлого периода (те же полигоны!) ---
    if progress_callback:
        await progress_callback(
            f"Расчёт очагов за прошлый год ({len(prev_cards)} ДТП)..."
        )
    prev_clusters, prev_preclusters, _polys = await _calc_cp(
        prev_cards,
        progress_callback,
        settlement_polygons=settlement_polygons,
    )

    if not prev_clusters:
        # За прошлый год очагов не найдено — все текущие = новые
        for c in current_clusters:
            c["dynamics"] = {
                "status": "new",
                "prev_total": None,
                "prev_deaths": None,
                "prev_injured": None,
                "match_distance": None,
            }
        logger.info(
            f"Динамика: за прошлый год очагов не найдено, "
            f"{len(current_clusters)} очагов помечены как новые"
        )
        if current_clusters:
            current_clusters[0]["_preclusters"] = current_preclusters
        # ВАЖНО: предочаги возвращаются отдельно — даже когда очагов нет
        return current_clusters, settlement_polygons, current_preclusters

    # --- Сопоставление ---
    if progress_callback:
        await progress_callback("Сопоставление очагов между периодами...")

    matches = _match_clusters(current_clusters, prev_clusters)

    # Аннотируем текущие очаги НОВОЙ структурой dynamics.
    # См. _match_clusters для описания алгоритма.
    # Структура dynamics:
    #   status: один из repeated_*/new/new_with_neighbor
    #   matched_prev_indices: [int, ...] — индексы в prev_clusters (для repeated)
    #   matched_prev_numbers: [int, ...] — номера в Excel-таблице (заполняются ниже)
    #   prev_total: суммарное ДТП всех сматченных прошлых (для repeated)
    #   prev_deaths, prev_injured: суммы
    #   match_distance: None (для repeated не нужен — есть пересечение)
    #   neighbors: [{prev_index, prev_number, distance_m}, ...] (для new_with_neighbor)
    for ci, curr in enumerate(current_clusters):
        matched_indices = matches.get(ci, [])
        neighbors_field = curr.pop("_neighbors", None)  # временное поле от _match_clusters

        if matched_indices:
            # === Повторный очаг ===
            matched_prevs = [prev_clusters[pi] for pi in matched_indices]
            prev_total = sum(p["total_accidents"] for p in matched_prevs)
            prev_deaths = sum(p["deaths"] for p in matched_prevs)
            prev_injured = sum(p["injured"] for p in matched_prevs)

            curr_total = curr["total_accidents"]

            if len(matched_indices) >= 2:
                # Слияние 2+ прошлогодних очагов
                status = "repeated_merged"
            elif curr_total > prev_total:
                status = "repeated_growing"
            elif curr_total < prev_total:
                status = "repeated_shrinking"
            else:
                status = "repeated_stable"

            curr["dynamics"] = {
                "status": status,
                "matched_prev_indices": list(matched_indices),
                "matched_prev_numbers": [],  # заполним после нумерации lost
                "prev_total": prev_total,
                "prev_deaths": prev_deaths,
                "prev_injured": prev_injured,
                "match_distance": None,
                "neighbors": [],
            }
        elif neighbors_field:
            # === Новый с соседом ===
            curr["dynamics"] = {
                "status": "new_with_neighbor",
                "matched_prev_indices": [],
                "matched_prev_numbers": [],
                "prev_total": None,
                "prev_deaths": None,
                "prev_injured": None,
                "match_distance": None,
                "neighbors": neighbors_field,  # [{prev_index, distance_m}, ...]
            }
        else:
            # === Новый без соседа ===
            curr["dynamics"] = {
                "status": "new",
                "matched_prev_indices": [],
                "matched_prev_numbers": [],
                "prev_total": None,
                "prev_deaths": None,
                "prev_injured": None,
                "match_distance": None,
                "neighbors": [],
            }

    # --- Прошлогодние очаги: исчезнувшие + повторённые ---
    # Прошлый очаг «исчез», если ни один текущий не сматчился с ним как повторный.
    # (Не путать с «соседом» — сосед не делает прошлый очаг не-исчезнувшим.)
    matched_prev_set: set[int] = set()
    # Какой текущий очаг (индекс в current_clusters) сматчился с этим prev
    # — нужно для prev_matched, чтобы показать «повторён в текущем №X».
    prev_to_curr_matchers: dict[int, list[int]] = {}
    for ci, indices in matches.items():
        if indices:  # только для повторных (не для соседей)
            matched_prev_set.update(indices)
            for pi in indices:
                prev_to_curr_matchers.setdefault(pi, []).append(ci)

    # Сначала добавим ПОВТОРЁННЫЕ прошлые очаги (как отдельные строки).
    # Раньше они вообще не попадали в Excel/карту — пользователь не видел,
    # какие именно очаги АППГ «превратились» в текущие. Теперь они
    # отображаются со статусом «АППГ (повторён)» и ссылкой на текущий №.
    matched_count = 0
    for pi, prev in enumerate(prev_clusters):
        if pi not in matched_prev_set:
            continue  # не повтора — обработаем ниже как lost
        matched_count += 1
        matched_prev_cluster = dict(prev)
        matched_prev_cluster["dynamics"] = {
            "status": "prev_matched",
            "matched_prev_indices": [],
            "matched_prev_numbers": [],
            "prev_total": prev["total_accidents"],
            "prev_deaths": prev["deaths"],
            "prev_injured": prev["injured"],
            "match_distance": None,
            "neighbors": [],
            # Индексы текущих очагов (в current_clusters ДО добавления
            # prev_matched/lost), которые сматчились с этим prev.
            # Заполняется числами (номерами в Excel) после нумерации.
            "matched_curr_indices": prev_to_curr_matchers.get(pi, []),
            "matched_curr_numbers": [],
        }
        matched_prev_cluster["_is_prev_matched"] = True
        matched_prev_cluster["_prev_index"] = pi
        current_clusters.append(matched_prev_cluster)

    # Теперь добавим ИСЧЕЗНУВШИЕ прошлые очаги (без матча в текущем).
    lost_count = 0
    for pi, prev in enumerate(prev_clusters):
        if pi in matched_prev_set:
            continue
        lost_count += 1
        lost_cluster = dict(prev)
        lost_cluster["dynamics"] = {
            "status": "lost",
            "matched_prev_indices": [],
            "matched_prev_numbers": [],
            "prev_total": prev["total_accidents"],
            "prev_deaths": prev["deaths"],
            "prev_injured": prev["injured"],
            "match_distance": None,
            "neighbors": [],
        }
        # Флаг для корректного отображения в Excel
        lost_cluster["_is_lost"] = True
        # Сохраняем оригинальный индекс в prev_clusters — нужен,
        # чтобы текущие очаги могли ссылаться на номер исчезнувшего.
        lost_cluster["_prev_index"] = pi
        current_clusters.append(lost_cluster)

    # --- Нумерация очагов для ссылок ---
    # В Excel-таблице у каждого очага есть № (1-based, по порядку в списке).
    # Порядок: текущие (1..N) → prev_matched (N+1..N+K) → lost (N+K+1..N+K+L).
    #
    # Строим маппинг: prev_index -> номер в Excel-таблице.
    # ВАЖНО: теперь включает КАК lost, ТАК И prev_matched — иначе
    # текущие очаги со статусом repeated не смогут сослаться на
    # «Да, №X» (X = номер prev_matched в таблице), и столбец
    # «Очаг в прошлом году» покажет «Нет» (баг #1 из production-логов).
    prev_index_to_excel_number: dict[int, int] = {}
    for i, c in enumerate(current_clusters, start=1):
        if c.get("_is_lost") or c.get("_is_prev_matched"):
            pi = c.get("_prev_index")
            if pi is not None:
                prev_index_to_excel_number[pi] = i

    # Заполняем matched_prev_numbers (для текущих) и matched_curr_numbers
    # (для prev_matched) и neighbors[].prev_number.
    # Чтобы сослаться на текущий очаг из prev_matched, нужен обратный
    # маппинг: curr_index -> excel_number. Текущие очаги идут первыми
    # (1..N), поэтому excel_number = curr_index + 1.
    n_current = len(current_clusters) - matched_count - lost_count
    for curr in current_clusters:
        dyn = curr.get("dynamics") or {}
        if not dyn:
            continue
        # matched_prev_numbers (для текущих очагов со статусом repeated_*)
        matched_indices = dyn.get("matched_prev_indices") or []
        if matched_indices:
            dyn["matched_prev_numbers"] = [
                prev_index_to_excel_number.get(pi)
                for pi in matched_indices
                if pi in prev_index_to_excel_number
            ]
        # matched_curr_numbers (для prev_matched — на какие текущие № ссылается)
        matched_curr_indices = dyn.get("matched_curr_indices") or []
        if matched_curr_indices:
            dyn["matched_curr_numbers"] = [
                ci + 1 for ci in matched_curr_indices  # 1-based
                if 0 <= ci < n_current
            ]
        # neighbors[].prev_number
        neighbors = dyn.get("neighbors") or []
        for n in neighbors:
            n["prev_number"] = prev_index_to_excel_number.get(n["prev_index"])

    # Статистика по новой методологии
    status_counts: dict[str, int] = {}
    for c in current_clusters:
        s = c.get("dynamics", {}).get("status", "new")
        status_counts[s] = status_counts.get(s, 0) + 1

    logger.info(
        f"Динамика очагов: "
        f"повторных={status_counts.get('repeated_growing', 0) + status_counts.get('repeated_shrinking', 0) + status_counts.get('repeated_stable', 0) + status_counts.get('repeated_merged', 0)}, "
        f"из них слияний={status_counts.get('repeated_merged', 0)}, "
        f"новых={status_counts.get('new', 0)}, "
        f"новых с соседом={status_counts.get('new_with_neighbor', 0)}, "
        f"повторённых АППГ={status_counts.get('prev_matched', 0)}, "
        f"исчезнувших={lost_count}, "
        f"всего={len(current_clusters)}"
    )

    # Backward-compat: предочаги в clusters[0]["_preclusters"] (если очаги есть)
    if current_clusters:
        current_clusters[0]["_preclusters"] = current_preclusters
    # ВАЖНО: предочаги возвращаются отдельно — даже когда очагов нет
    return current_clusters, settlement_polygons, current_preclusters


# ========================
# Excel-выход: динамика
# ========================

DYNAMICS_COLUMNS = [
    "№ очага",
    "Статус",
    "Тип зоны",
    "Дорога/Улица",
    "Пикетаж начало",
    "Пикетаж конец",
    "Широта",
    "Долгота",
    "Кол-во ДТП",
    "ДТП (пр. период)",
    "Изменение ДТП",
    # Новые столбцы (методология пикетаж + сосед)
    "Очаг в прошлом году",
    "Соседние очаги (пр. период)",
    # Для prev_matched: на какой текущий № ссылается этот АППГ-очаг
    "Повторён в текущем",
    "Виды ДТП (детализация)",
    "Доминирующий вид",
    "Погибло",
    "Ранено",
    "Погибло (пр. период)",
    "Ранено (пр. период)",
    "Дата первого ДТП",
    "Дата последнего ДТП",
]

DYNAMICS_DETAIL_COLUMNS = [
    "№ очага",
    "Статус",
    "Период",
    "Дата ДТП",
    "Вид ДТП",
    "Дорога/Улица",
    "Пикетаж",
    "Широта",
    "Долгота",
    "Погибло",
    "Ранено",
    # --- Причины ДТП (по каждому ДТП) ---
    "Непосредственные нарушения ПДД",
    "Сопутствующие нарушения (опьянение/лишение)",
    "Недостатки ТЭС",
    "Состояние проезжей части",
    "Фактор режима движения",
    "Технические неисправности",
    "Освещение",
]


