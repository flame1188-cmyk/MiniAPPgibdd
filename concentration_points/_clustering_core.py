"""Алгоритмы кластеризации — населённые пункты и вне НП."""
import logging
from collections import Counter

from shapely.geometry import Polygon, MultiPolygon

from ._constants import (
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
from ._osm_boundaries import classify_cards
from analytics import _safe_int

logger = logging.getLogger(__name__)

def _check_cluster_criteria(
    type_counter: Counter,
    total: int,
) -> tuple[bool, str | None]:
    """
    Проверяет, выполняется ли критерий очага.

    Returns:
        (is_cluster, dominant_type)
        dominant_type — вид ДТП, достигший порога 3+, или None при пороге 5+
    """
    for dtp_type, count in type_counter.most_common():
        if count >= SAME_TYPE_THRESHOLD:
            return True, dtp_type
    if total >= ANY_TYPE_THRESHOLD:
        return True, None
    return False, None


def _check_precluster_criteria(
    type_counter: Counter,
    total: int,
) -> tuple[bool, str | None]:
    """
    Проверяет, выполняется ли критерий предочага.

    Предочаг — место, которому не хватает 1 ДТП до очага:
      - 2+ ДТП одного вида (до порога 3)
      - 4+ ДТП любых видов (до порога 5)

    Returns:
        (is_precluster, dominant_type)
    """
    for dtp_type, count in type_counter.most_common():
        if count >= PRE_SAME_TYPE_THRESHOLD:
            return True, dtp_type
    if total >= PRE_ANY_TYPE_THRESHOLD:
        return True, None
    return False, None


# ========================
# Кэширование границ НП
# ========================

# ========================
# In-memory кэш полигонов
# ========================

def _matches_sop_filter(text: str) -> bool:
    """Проверяет, содержит ли сопутствующее нарушение ключевые слова."""
    lower = text.lower()
    return any(w in lower for w in _SOP_NPDD_FILTER_WORDS)


def _format_counter(counter: dict[str, int], top_n: int = 5) -> str:
    """Форматирует счётчик в строку: \"Значение 1 (5); Значение 2 (3)\"."""
    if not counter:
        return ""
    parts = [
        f"{k} ({v})"
        for k, v in sorted(counter.items(), key=lambda x: -x[1])[:top_n]
    ]
    return "; ".join(parts)


def _build_cause_counters(cards: list[dict]) -> dict[str, dict[str, int]]:
    """Строит счётчики причин ДТП по всем карточкам очага.

    Возвращает dict с 6 счётчиками:
      npdd_counter  — Непосредственные нарушения ПДД
      sop_npdd_counter — Сопутствующие нарушения (фильтр: опьянение/лишенным/имеющим)
      ndu_counter   — Недостатки транспортно-эксплуатационного содержания
      spch_counter  — Состояние проезжей части
      factor_counter — Фактор режима движения
      tn_counter    — Технические неисправности
    """
    npdd_counter: Counter = Counter()
    sop_npdd_counter: Counter = Counter()
    ndu_counter: Counter = Counter()
    spch_counter: Counter = Counter()
    factor_counter: Counter = Counter()
    tn_counter: Counter = Counter()

    for c in cards:
        # --- dor_usl (уровень карточки) ---
        dor_usl = c.get("dor_usl") or {}
        if isinstance(dor_usl, dict):
            for item in (dor_usl.get("ndu") or []):
                s = str(item).strip()
                if s and s.lower() not in _CAUSE_SKIP_VALUES:
                    ndu_counter[s] += 1
            spch_val = str(dor_usl.get("s_pch", "")).strip()
            if spch_val:
                spch_counter[spch_val] += 1
            for item in (dor_usl.get("factor") or []):
                s = str(item).strip()
                if s and s.lower() not in _CAUSE_SKIP_VALUES:
                    factor_counter[s] += 1

        # --- ts_info → ts_uch (уровень участников-водителей) ---
        for ts in (c.get("ts_info") or []):
            # Технические неисправности ТС
            tn_val = str(ts.get("t_n", "")).strip()
            if tn_val and tn_val.lower() not in _CAUSE_SKIP_VALUES:
                tn_counter[tn_val] += 1
            for uch in (ts.get("ts_uch") or []):
                for v in (uch.get("npdd") or []):
                    s = str(v).strip()
                    if s and s.lower() not in _CAUSE_SKIP_VALUES:
                        npdd_counter[s] += 1
                for v in (uch.get("sop_npdd") or []):
                    s = str(v).strip()
                    if s and _matches_sop_filter(s):
                        sop_npdd_counter[s] += 1

        # --- uch_info (пешеходы и прочие участники) ---
        for uch in (c.get("uch_info") or []):
            for v in (uch.get("npdd") or []):
                s = str(v).strip()
                if s and s.lower() not in _CAUSE_SKIP_VALUES:
                    npdd_counter[s] += 1
            for v in (uch.get("sop_npdd") or []):
                s = str(v).strip()
                if s and _matches_sop_filter(s):
                    sop_npdd_counter[s] += 1

    return {
        "npdd_counter": dict(npdd_counter),
        "sop_npdd_counter": dict(sop_npdd_counter),
        "ndu_counter": dict(ndu_counter),
        "spch_counter": dict(spch_counter),
        "factor_counter": dict(factor_counter),
        "tn_counter": dict(tn_counter),
    }


# ========================
# Алгоритм: НП (перекрёстки 50 м, участки 100 м)
# ========================

def _build_cluster(
    cards: list[dict],
    center: tuple[float, float] | None,
    zone_type: str,
    road_name: str = "",
    start_pos: float | None = None,
    end_pos: float | None = None,
) -> dict:
    """Формирует словарь очага из группы карточек."""
    total_deaths = sum(_safe_int(c.get("pog")) for c in cards)
    total_injured = sum(_safe_int(c.get("ran")) for c in cards)
    dates = [_get_date(c) for c in cards]
    type_counter = Counter(_get_dtp_type(c) for c in cards)
    cause_counters = _build_cause_counters(cards)

    dominant = None
    for t, cnt in type_counter.most_common():
        if cnt >= SAME_TYPE_THRESHOLD:
            dominant = t
            break

    road = road_name or _get_road_name(cards[0])

    first_coords = _parse_coords(cards[0])
    last_coords = _parse_coords(cards[-1])

    # Реальные границы очага по пикетажу ДТП (min/max из всех карточек)
    dtp_piketazh_positions = [_get_km_m(c) for c in cards]
    dtp_piketazh_positions = [p for p in dtp_piketazh_positions if p is not None]
    if dtp_piketazh_positions:
        dtp_pk_min = min(dtp_piketazh_positions)
        dtp_pk_max = max(dtp_piketazh_positions)
    else:
        dtp_pk_min = None
        dtp_pk_max = None

    return {
        "zone_type": zone_type,
        "road": road,
        "total_accidents": len(cards),
        "deaths": total_deaths,
        "injured": total_injured,
        "dates": dates,
        "type_counter": dict(type_counter),
        "dominant_type": dominant,
        "first_coords": first_coords,
        "last_coords": last_coords,
        "center": center or first_coords or (0, 0),
        "start_pos": start_pos,
        "end_pos": end_pos,
        "cards": cards,
        # Поля для camera_matcher (окно группировки — для поиска "ближайших")
        "has_piketazh": start_pos is not None,
        "start_km": start_pos,
        "end_km": end_pos,
        # Реальные границы очага по ДТП (для определения "закрыт")
        "dtp_pk_min": dtp_pk_min,
        "dtp_pk_max": dtp_pk_max,
        # Причины ДТП (счётчики для Excel и LLM)
        **cause_counters,
    }


def _build_precluster(
    cards: list[dict],
    center: tuple[float, float] | None,
    zone_type: str,
    road_name: str = "",
    start_pos: float | None = None,
    end_pos: float | None = None,
) -> dict:
    """Формирует словарь предочага из группы карточек."""
    total_deaths = sum(_safe_int(c.get("pog")) for c in cards)
    total_injured = sum(_safe_int(c.get("ran")) for c in cards)
    dates = [_get_date(c) for c in cards]
    type_counter = Counter(_get_dtp_type(c) for c in cards)
    cause_counters = _build_cause_counters(cards)

    dominant = None
    for t, cnt in type_counter.most_common():
        if cnt >= PRE_SAME_TYPE_THRESHOLD:
            dominant = t
            break

    road = road_name or _get_road_name(cards[0])

    first_coords = _parse_coords(cards[0])
    last_coords = _parse_coords(cards[-1])

    # Реальные границы предочага по пикетажу ДТП
    dtp_piketazh_positions = [_get_km_m(c) for c in cards]
    dtp_piketazh_positions = [p for p in dtp_piketazh_positions if p is not None]
    if dtp_piketazh_positions:
        dtp_pk_min = min(dtp_piketazh_positions)
        dtp_pk_max = max(dtp_piketazh_positions)
    else:
        dtp_pk_min = None
        dtp_pk_max = None

    # Определяем критерий, по которому сработал предочаг
    max_same = max(type_counter.values()) if type_counter else 0
    if max_same >= PRE_SAME_TYPE_THRESHOLD:
        criterion = f"{max_same} ДТП одного вида"
    else:
        criterion = f"{len(cards)} ДТП разных видов"

    return {
        "zone_type": zone_type,
        "road": road,
        "total_accidents": len(cards),
        "deaths": total_deaths,
        "injured": total_injured,
        "dates": dates,
        "type_counter": dict(type_counter),
        "dominant_type": dominant,
        "first_coords": first_coords,
        "last_coords": last_coords,
        "center": center or first_coords or (0, 0),
        "start_pos": start_pos,
        "end_pos": end_pos,
        "cards": cards,
        "has_piketazh": start_pos is not None,
        "start_km": start_pos,
        "end_km": end_pos,
        "dtp_pk_min": dtp_pk_min,
        "dtp_pk_max": dtp_pk_max,
        # Специфичные поля предочага
        "is_precluster": True,
        "precluster_criterion": criterion,
        # Причины ДТП (счётчики для Excel и LLM)
        **cause_counters,
    }


def _cluster_cards_by_radius(
    cards_with_idx: list[tuple[int, dict]],
    radius_m: float,
    assigned: set[int],
) -> list[int] | None:
    """
    Для карточки cards_with_idx[0] ищет все карточки в радиусе radius_m.
    Если порог очага выполнен — возвращает список индексов (включая центральный),
    иначе None.
    """
    if not cards_with_idx:
        return None

    first_idx, first_card = cards_with_idx[0]
    center = _parse_coords(first_card)
    if center is None:
        return None

    group_indices = [first_idx]
    group_cards = [first_card]

    for idx, card in cards_with_idx[1:]:
        if idx in assigned:
            continue
        coords = _parse_coords(card)
        if coords is None:
            continue
        dist = haversine_meters(
            center[0], center[1], coords[0], coords[1],
        )
        if dist <= radius_m:
            group_indices.append(idx)
            group_cards.append(card)

    type_counter = Counter(_get_dtp_type(c) for c in group_cards)
    is_cluster, _ = _check_cluster_criteria(type_counter, len(group_cards))

    if is_cluster:
        return group_indices
    return None


def _extract_assigned_indices(
    clusters: list[dict],
    cards: list[dict],
) -> set[int]:
    """Извлекает множество индексов карточек, вошедших в кластеры.

    Используется для передачи в find_*_preclusters, чтобы предочаги
    не включали карточки уже из очагов.
    """
    # Строим map: id(card) -> index в списке cards
    id_to_idx = {id(c): i for i, c in enumerate(cards)}
    assigned: set[int] = set()
    for cluster in clusters:
        for card in cluster.get("cards", []):
            idx = id_to_idx.get(id(card))
            if idx is not None:
                assigned.add(idx)
    return assigned


def find_settlement_preclusters(
    cards: list[dict],
    cluster_assigned: set[int],
) -> list[dict]:
    """
    Поиск предочагов в населённых пунктах.

    Алгоритм идентичен find_settlement_concentration_points (3 прохода),
    но:
    - Использует _check_precluster_criteria (2 одного вида / 4 разных)
    - Исключает карточки, уже вошедшие в очаги (cluster_assigned)
    - Карточки, вошедшие в предочаг, тоже помечаются (pre_assigned),
      чтобы не дублироваться
    """
    if not cards:
        return []

    indexed = [(i, c) for i, c in enumerate(cards)]
    indexed.sort(key=lambda x: _get_date(x[1]))
    indexed_with_coords = [(i, c) for i, c in indexed if _parse_coords(c)]

    assigned: set[int] = set(cluster_assigned)  # исключаем очаговые
    preclusters: list[dict] = []

    # --- 1-й проход: перекрёстки (50 м) ---

    # Шаг 1a: Перекрёстки С дорогой+пикетажем
    for idx, card in indexed_with_coords:
        if idx in assigned:
            continue
        if not _is_intersection(card):
            continue
        if not _has_road_and_piketazh(card):
            continue

        center_road = _get_road_name(card)
        center_km = _get_km_m(card)
        center = _parse_coords(card)
        if center is None:
            continue

        # 1a-1: По пикетажу
        piketazh_candidates = []
        for j, c in indexed_with_coords:
            if j in assigned or j == idx:
                continue
            if _get_road_name(c) != center_road:
                continue
            other_km = _get_km_m(c)
            if other_km is None:
                continue
            if abs(center_km - other_km) * 1000.0 > SETTLEMENT_INTERSECTION_RADIUS_M:
                continue
            if not _is_intersection(c):
                continue
            piketazh_candidates.append((j, c))

        if piketazh_candidates:
            group_cards = [card] + [c for _, c in piketazh_candidates]
            type_counter = Counter(_get_dtp_type(c) for c in group_cards)
            is_pre, _ = _check_precluster_criteria(type_counter, len(group_cards))
            if is_pre:
                assigned.add(idx)
                for j, _ in piketazh_candidates:
                    assigned.add(j)
                group_cards.sort(key=lambda c: _get_date(c))
                preclusters.append(
                    _build_precluster(group_cards, center, "settlement_intersection")
                )
                continue

        # 1a-2: Fallback — радиус 50 м по GPS
        gps_candidates = [
            (j, c) for j, c in indexed_with_coords
            if j not in assigned and j != idx
        ]

        group_indices = [idx]
        group_cards = [card]

        for j, c in gps_candidates:
            if not _is_intersection(c):
                continue
            coords = _parse_coords(c)
            if coords is None:
                continue
            dist = haversine_meters(center[0], center[1], coords[0], coords[1])
            if dist > SETTLEMENT_INTERSECTION_RADIUS_M:
                continue

            other_road = _get_road_name(c)
            other_km = _get_km_m(c)
            if other_road == center_road and other_km is not None:
                piketazh_diff_m = abs(other_km - center_km) * 1000.0
                if piketazh_diff_m > SETTLEMENT_INTERSECTION_RADIUS_M:
                    continue

            group_indices.append(j)
            group_cards.append(c)

        type_counter = Counter(_get_dtp_type(c) for c in group_cards)
        is_pre, _ = _check_precluster_criteria(type_counter, len(group_cards))
        if is_pre:
            assigned.update(group_indices)
            group_cards.sort(key=lambda c: _get_date(c))
            preclusters.append(
                _build_precluster(group_cards, center, "settlement_intersection")
            )

    # Шаг 1b: Перекрёстки БЕЗ пикетажа
    for idx, card in indexed_with_coords:
        if idx in assigned:
            continue
        if not _is_intersection(card):
            continue
        if _has_road_and_piketazh(card):
            continue

        center = _parse_coords(card)
        if center is None:
            continue

        group_indices = [idx]
        group_cards = [card]

        for j, c in indexed_with_coords:
            if j in assigned or j == idx:
                continue
            if not _is_intersection(c):
                continue
            coords = _parse_coords(c)
            if coords is None:
                continue
            dist = haversine_meters(center[0], center[1], coords[0], coords[1])
            if dist <= SETTLEMENT_INTERSECTION_RADIUS_M:
                group_indices.append(j)
                group_cards.append(c)

        type_counter = Counter(_get_dtp_type(c) for c in group_cards)
        is_pre, _ = _check_precluster_criteria(type_counter, len(group_cards))

        if is_pre:
            assigned.update(group_indices)
            group_cards.sort(key=lambda c: _get_date(c))
            preclusters.append(
                _build_precluster(group_cards, center, "settlement_intersection")
            )

    # --- 2-й проход: дороги с пикетажем, окно 200 м ---
    road_cards_with_km = [
        (idx, card) for idx, card in indexed_with_coords
        if idx not in assigned and _has_road_and_piketazh(card)
    ]

    road_groups: dict[str, list[tuple[int, dict]]] = {}
    for idx, card in road_cards_with_km:
        road = _get_road_name(card)
        road_groups.setdefault(road, []).append((idx, card))

    for road_name, items in road_groups.items():
        items_pos: list[tuple[int, dict, float]] = []
        for idx, card in items:
            pos = _get_km_m(card)
            if pos is not None:
                items_pos.append((idx, card, pos))

        if not items_pos:
            continue

        items_pos.sort(key=lambda x: x[2])

        for i, (idx, card, pos) in enumerate(items_pos):
            if idx in assigned:
                continue

            window_end = pos + SETTLEMENT_ROAD_WINDOW_KM

            group_indices = [idx]
            group_cards = [card]

            center_coords = _parse_coords(card)

            for j in range(i + 1, len(items_pos)):
                other_idx, other_card, other_pos = items_pos[j]
                if other_idx in assigned:
                    continue
                if other_pos > window_end:
                    break
                if center_coords:
                    other_coords = _parse_coords(other_card)
                    if other_coords:
                        gps_dist = haversine_meters(
                            center_coords[0], center_coords[1],
                            other_coords[0], other_coords[1],
                        )
                        if gps_dist > SETTLEMENT_ROAD_GPS_MAX_M:
                            continue
                group_indices.append(other_idx)
                group_cards.append(other_card)

            type_counter = Counter(_get_dtp_type(c) for c in group_cards)
            is_pre, _ = _check_precluster_criteria(type_counter, len(group_cards))

            if is_pre:
                assigned.update(group_indices)
                group_cards.sort(key=lambda c: _get_date(c))
                center = _parse_coords(card)
                preclusters.append(
                    _build_precluster(
                        group_cards, center, "settlement_road",
                        road_name=road_name,
                        start_pos=pos,
                        end_pos=window_end,
                    )
                )

    # --- 3-й проход: радиус 100 м ---
    for idx, card in indexed_with_coords:
        if idx in assigned:
            continue

        center = _parse_coords(card)
        if center is None:
            assigned.add(idx)
            continue

        center_road = _get_road_name(card)
        center_km = _get_km_m(card)
        center_has_road_km = bool(center_road) and center_km is not None

        candidates = [
            (j, c) for j, c in indexed_with_coords
            if j not in assigned and j != idx
        ]

        group_indices = [idx]
        group_cards = [card]

        for j, c in candidates:
            coords = _parse_coords(c)
            if coords is None:
                continue
            dist = haversine_meters(center[0], center[1], coords[0], coords[1])
            if dist > SETTLEMENT_OTHER_RADIUS_M:
                continue

            other_road = _get_road_name(c)
            other_km = _get_km_m(c)

            if (
                center_has_road_km
                and other_road == center_road
                and other_km is not None
            ):
                piketazh_diff_m = abs(other_km - center_km) * 1000.0
                if piketazh_diff_m > SETTLEMENT_ROAD_WINDOW_KM * 1000.0:
                    continue

            group_indices.append(j)
            group_cards.append(c)

        type_counter = Counter(_get_dtp_type(c) for c in group_cards)
        is_pre, _ = _check_precluster_criteria(type_counter, len(group_cards))

        if is_pre:
            assigned.update(group_indices)
            group_cards.sort(key=lambda c: _get_date(c))
            preclusters.append(
                _build_precluster(group_cards, center, "settlement_segment")
            )
        else:
            assigned.add(idx)

    logger.info(f"Предочаги в НП: {len(preclusters)} найдено")
    return preclusters


def find_settlement_concentration_points(cards: list[dict]) -> list[dict]:
    """
    Поиск очагов в населённых пунктах — 3 прохода.

    1-й проход: перекрёстки (50 м) с проверкой пикетажа:
      Шаг 1a: ДТП с дорогой+пикетажем — сначала по пикетажу (±50 м),
              затем fallback радиус 50 м по GPS с piketаж-фильтром
      Шаг 1b: ДТП без пикетажа — стандартный радиус 50 м по GPS
    2-й проход: дороги с наименованием + пикетажем, окно 200 м
    3-й проход: радиус 100 м с проверкой пикетажа (200 м для ДТП
               с одинаковой дорогой и пикетажем)
    """
    if not cards:
        return []

    # Подготавливаем: индекс + карточка, сортируем по дате
    indexed = [(i, c) for i, c in enumerate(cards)]
    indexed.sort(key=lambda x: _get_date(x[1]))

    # Фильтруем только карточки с координатами
    indexed_with_coords = [(i, c) for i, c in indexed if _parse_coords(c)]

    assigned: set[int] = set()
    clusters: list[dict] = []

    # --- 1-й проход: перекрёстки (50 м) с проверкой пикетажа ---

    # Шаг 1a: Перекрёстки С наименованием дороги и пикетажем
    for idx, card in indexed_with_coords:
        if idx in assigned:
            continue
        if not _is_intersection(card):
            continue
        if not _has_road_and_piketazh(card):
            continue

        center_road = _get_road_name(card)
        center_km = _get_km_m(card)
        center = _parse_coords(card)
        if center is None:
            continue

        # 1a-1: Проверка по пикетажу: ±50 м по той же дороге,
        #        только ДТП с «перекрёсток»
        piketazh_candidates = []
        for j, c in indexed_with_coords:
            if j in assigned or j == idx:
                continue
            if _get_road_name(c) != center_road:
                continue
            other_km = _get_km_m(c)
            if other_km is None:
                continue
            if abs(center_km - other_km) * 1000.0 > SETTLEMENT_INTERSECTION_RADIUS_M:
                continue
            if not _is_intersection(c):
                continue
            piketazh_candidates.append((j, c))

        if piketazh_candidates:
            group_cards = [card] + [c for _, c in piketazh_candidates]
            type_counter = Counter(
                _get_dtp_type(c) for c in group_cards
            )
            is_cluster, _ = _check_cluster_criteria(
                type_counter, len(group_cards),
            )
            if is_cluster:
                assigned.add(idx)
                for j, _ in piketazh_candidates:
                    assigned.add(j)
                group_cards.sort(key=lambda c: _get_date(c))
                clusters.append(
                    _build_cluster(
                        group_cards, center, "settlement_intersection"
                    )
                )
                continue

        # 1a-2: Fallback — радиус 50 м по GPS (только «перекрёстки»),
        #        с проверкой пикетажа для ДТП на той же дороге
        gps_candidates = [
            (j, c) for j, c in indexed_with_coords
            if j not in assigned and j != idx
        ]

        group_indices = [idx]
        group_cards = [card]

        for j, c in gps_candidates:
            if not _is_intersection(c):
                continue
            coords = _parse_coords(c)
            if coords is None:
                continue
            dist = haversine_meters(
                center[0], center[1], coords[0], coords[1],
            )
            if dist > SETTLEMENT_INTERSECTION_RADIUS_M:
                continue

            # Проверка пикетажа: если ДТП на той же дороге
            # и имеет пикетаж — проверяем окно 50 м
            other_road = _get_road_name(c)
            other_km = _get_km_m(c)
            if (
                other_road == center_road
                and other_km is not None
            ):
                piketazh_diff_m = abs(other_km - center_km) * 1000.0
                if piketazh_diff_m > SETTLEMENT_INTERSECTION_RADIUS_M:
                    # Пикетаж различается более чем на 50 м — исключаем
                    continue

            group_indices.append(j)
            group_cards.append(c)

        type_counter = Counter(
            _get_dtp_type(c) for c in group_cards
        )
        is_cluster, _ = _check_cluster_criteria(
            type_counter, len(group_cards),
        )
        if is_cluster:
            assigned.update(group_indices)
            group_cards.sort(key=lambda c: _get_date(c))
            clusters.append(
                _build_cluster(
                    group_cards, center, "settlement_intersection"
                )
            )

    # Шаг 1b: Перекрёстки БЕЗ пикетажа — радиус 50 м по GPS
    # (с пикетажем уже обработаны в шаге 1a)
    # Кандидаты должны быть тоже «перекрёстками» (sdor)
    for idx, card in indexed_with_coords:
        if idx in assigned:
            continue
        if not _is_intersection(card):
            continue
        if _has_road_and_piketazh(card):
            continue  # уже обработаны в шаге 1a

        center = _parse_coords(card)
        if center is None:
            continue

        # Собираем кандидатов в радиусе 50 м (только «перекрёстки»)
        group_indices = [idx]
        group_cards = [card]

        for j, c in indexed_with_coords:
            if j in assigned or j == idx:
                continue
            if not _is_intersection(c):
                continue
            coords = _parse_coords(c)
            if coords is None:
                continue
            dist = haversine_meters(
                center[0], center[1], coords[0], coords[1],
            )
            if dist <= SETTLEMENT_INTERSECTION_RADIUS_M:
                group_indices.append(j)
                group_cards.append(c)

        type_counter = Counter(
            _get_dtp_type(c) for c in group_cards
        )
        is_cluster, _ = _check_cluster_criteria(
            type_counter, len(group_cards),
        )

        if is_cluster:
            assigned.update(group_indices)
            group_cards.sort(key=lambda c: _get_date(c))
            clusters.append(
                _build_cluster(group_cards, center, "settlement_intersection")
            )

    # --- 2-й проход: дороги с наименованием и пикетажем, окно 200 м ---
    road_cards_with_km = [
        (idx, card) for idx, card in indexed_with_coords
        if idx not in assigned and _has_road_and_piketazh(card)
    ]

    # Группируем по названию дороги
    road_groups: dict[str, list[tuple[int, dict]]] = {}
    for idx, card in road_cards_with_km:
        road = _get_road_name(card)
        road_groups.setdefault(road, []).append((idx, card))

    pass2_found = False

    for road_name, items in road_groups.items():
        # Подготавливаем (idx, card, pos_km)
        items_pos: list[tuple[int, dict, float]] = []
        for idx, card in items:
            pos = _get_km_m(card)
            if pos is not None:
                items_pos.append((idx, card, pos))

        if not items_pos:
            continue

        # Сортируем по пикетажу
        items_pos.sort(key=lambda x: x[2])

        # Скользящее окно 200 м
        for i, (idx, card, pos) in enumerate(items_pos):
            if idx in assigned:
                continue

            window_end = pos + SETTLEMENT_ROAD_WINDOW_KM

            group_indices = [idx]
            group_cards = [card]

            center_coords = _parse_coords(card)

            for j in range(i + 1, len(items_pos)):
                other_idx, other_card, other_pos = items_pos[j]
                if other_idx in assigned:
                    continue
                if other_pos > window_end:
                    break
                if center_coords:
                    other_coords = _parse_coords(other_card)
                    if other_coords:
                        gps_dist = haversine_meters(
                            center_coords[0], center_coords[1],
                            other_coords[0], other_coords[1],
                        )
                        if gps_dist > SETTLEMENT_ROAD_GPS_MAX_M:
                            continue
                group_indices.append(other_idx)
                group_cards.append(other_card)

            type_counter = Counter(_get_dtp_type(c) for c in group_cards)
            is_cluster, _ = _check_cluster_criteria(
                type_counter, len(group_cards),
            )

            if is_cluster:
                assigned.update(group_indices)
                group_cards.sort(key=lambda c: _get_date(c))
                center = _parse_coords(card)
                clusters.append(
                    _build_cluster(
                        group_cards, center, "settlement_road",
                        road_name=road_name,
                        start_pos=pos,
                        end_pos=window_end,
                    )
                )
                pass2_found = True
            # Неассигнированные карточки переходят в 3-й проход

    logger.info(
        f"НП 2-й проход (пикетаж): "
        f"{len(clusters)} очагов найдено" if pass2_found
        else "НП 2-й проход: очагов не найдено"
    )

    # --- 3-й проход: радиус 100 м с проверкой пикетажа ---
    for idx, card in indexed_with_coords:
        if idx in assigned:
            continue

        center = _parse_coords(card)
        if center is None:
            assigned.add(idx)
            continue

        center_road = _get_road_name(card)
        center_km = _get_km_m(card)
        center_has_road_km = bool(center_road) and center_km is not None

        # Собираем кандидатов в радиусе 100 м
        candidates = [
            (j, c) for j, c in indexed_with_coords
            if j not in assigned and j != idx
        ]

        group_indices = [idx]
        group_cards = [card]

        for j, c in candidates:
            coords = _parse_coords(c)
            if coords is None:
                continue
            dist = haversine_meters(
                center[0], center[1], coords[0], coords[1],
            )
            if dist > SETTLEMENT_OTHER_RADIUS_M:
                continue

            # Проверка пикетажа: если центр и кандидат на одной дороге
            # и оба имеют пикетаж — проверяем окно 200 м
            other_road = _get_road_name(c)
            other_km = _get_km_m(c)

            if (
                center_has_road_km
                and other_road == center_road
                and other_km is not None
            ):
                piketazh_diff_m = abs(other_km - center_km) * 1000.0
                if piketazh_diff_m > SETTLEMENT_ROAD_WINDOW_KM * 1000.0:
                    # Пикетаж различается более чем на 200 м — исключаем
                    continue

            group_indices.append(j)
            group_cards.append(c)

        type_counter = Counter(_get_dtp_type(c) for c in group_cards)
        is_cluster, _ = _check_cluster_criteria(type_counter, len(group_cards))

        if is_cluster:
            assigned.update(group_indices)
            group_cards.sort(key=lambda c: _get_date(c))
            clusters.append(
                _build_cluster(group_cards, center, "settlement_segment")
            )
        else:
            assigned.add(idx)

    logger.info(f"Очаги в НП (итого): {len(clusters)} найдено")
    return clusters


# ========================
# Алгоритм: Вне НП (окна 1 км по дорогам)
# ========================


def find_nonsettlement_preclusters(
    cards: list[dict],
    cluster_assigned: set[int],
) -> list[dict]:
    """
    Поиск предочагов вне населённых пунктов.

    Алгоритм идентичен find_nonsettlement_concentration_points (окна по дорогам),
    но использует _check_precluster_criteria и исключает карточки из очагов.
    """
    if not cards:
        return []

    road_groups: dict[str, list[dict]] = {}
    for i, card in enumerate(cards):
        if i in cluster_assigned:
            continue
        road = _get_road_name(card)
        if not road:
            continue
        road_groups.setdefault(road, []).append((i, card))

    all_preclusters: list[dict] = []

    for road_name, road_items in road_groups.items():
        cards_pos: list[tuple[int, dict, float, tuple | None]] = []
        for i, card in road_items:
            pos = _get_km_m(card)
            coords = _parse_coords(card)
            if pos is not None:
                cards_pos.append((i, card, pos, coords))
            elif coords is not None:
                cards_pos.append((i, card, 0.0, coords))

        if not cards_pos:
            continue

        ref_coords = None
        for _, _, _, coords in cards_pos:
            if coords:
                ref_coords = coords
                break
        if ref_coords is None:
            continue

        # Пересчитываем позиции для карточек без km/m
        for k, (i, card, pos, coords) in enumerate(cards_pos):
            if pos == 0.0 and _get_km_m(card) is None and coords:
                dist_km = haversine_meters(
                    ref_coords[0], ref_coords[1],
                    coords[0], coords[1],
                ) / 1000.0
                cards_pos[k] = (i, card, dist_km, coords)

        cards_pos.sort(key=lambda x: (x[2], _get_date(x[1])))

        has_piketazh = any(_get_km_m(card) is not None for _, card, _, _ in cards_pos)
        window_km = NON_SETTLEMENT_WINDOW_KM if has_piketazh else NON_SETTLEMENT_NO_PK_WINDOW_KM

        assigned: set[int] = set()  # ki — индексы внутри cards_pos

        for ki, (i, card, pos, coords) in enumerate(cards_pos):
            if ki in assigned:
                continue

            window_start = pos
            window_end = pos + window_km

            group_indices = [ki]
            group_cards = [card]

            for kj, (oj, other_card, other_pos, other_coords) in enumerate(cards_pos):
                if kj in assigned or kj == ki:
                    continue
                if window_start <= other_pos <= window_end:
                    group_indices.append(kj)
                    group_cards.append(other_card)

            type_counter = Counter(_get_dtp_type(c) for c in group_cards)
            is_pre, _ = _check_precluster_criteria(type_counter, len(group_cards))

            if is_pre and _max_gps_spread(group_cards) <= NON_SETTLEMENT_GPS_MAX_SPREAD_M:
                assigned.update(group_indices)
                group_cards.sort(key=lambda c: _get_date(c))

                first_coords = _parse_coords(group_cards[0])
                last_coords = _parse_coords(group_cards[-1])

                # Если все ДТП в предочаге — перекрёстки, помечаем как перекрёсток
                all_intersection = all(_is_intersection(c) for c in group_cards)
                zone = "nonsettlement_intersection" if all_intersection else "nonsettlement"

                all_preclusters.append(
                    _build_precluster(
                        group_cards, coords or first_coords, zone,
                        road_name=road_name,
                        start_pos=window_start,
                        end_pos=window_end,
                    )
                )
            else:
                assigned.add(ki)

    logger.info(f"Предочаги вне НП: {len(all_preclusters)} найдено")
    return all_preclusters


def find_nonsettlement_concentration_points(cards: list[dict]) -> list[dict]:
    """
    Поиск очагов вне населённых пунктов.

    1. Группировка по названию дороги (поле dor)
    2. Сортировка по пикетажу (km+m) или по координатам
    3. Скользящее окно 1 км
    """
    if not cards:
        return []

    # Группируем по дороге
    road_groups: dict[str, list[dict]] = {}
    for card in cards:
        road = _get_road_name(card)
        if not road:
            continue
        road_groups.setdefault(road, []).append(card)

    all_clusters: list[dict] = []

    for road_name, road_cards in road_groups.items():
        # Подготавливаем: (card, position_km, coords)
        cards_pos: list[tuple[dict, float, tuple | None]] = []
        for card in road_cards:
            pos = _get_km_m(card)
            coords = _parse_coords(card)
            if pos is not None:
                cards_pos.append((card, pos, coords))
            elif coords is not None:
                cards_pos.append((card, 0.0, coords))  # позиция вычислим ниже

        if not cards_pos:
            continue

        # Если есть карточки без пикетажа — вычисляем по координатам
        ref_coords = None
        for card, pos, coords in cards_pos:
            if coords:
                ref_coords = coords
                break

        if ref_coords is None:
            continue

        # Пересчитываем позиции для карточек без km/m
        for i, (card, pos, coords) in enumerate(cards_pos):
            if pos == 0.0 and _get_km_m(card) is None and coords:
                dist_km = haversine_meters(
                    ref_coords[0], ref_coords[1],
                    coords[0], coords[1],
                ) / 1000.0
                cards_pos[i] = (card, dist_km, coords)

        # Сортируем по позиции, затем по дате
        cards_pos.sort(key=lambda x: (x[1], _get_date(x[0])))

        # Определяем окно: если на дороге есть хотя бы одно ДТП с пикетажем — 1 км,
        # если ни одного — 200 м (расчёт по GPS менее точен)
        has_piketazh = any(_get_km_m(card) is not None for card, _, _ in cards_pos)
        window_km = NON_SETTLEMENT_WINDOW_KM if has_piketazh else NON_SETTLEMENT_NO_PK_WINDOW_KM

        # Скользящее окно
        assigned: set[int] = set()

        for i, (card, pos, coords) in enumerate(cards_pos):
            if i in assigned:
                continue

            window_start = pos
            window_end = pos + window_km

            group_indices = [i]
            group_cards = [card]

            for j, (other_card, other_pos, other_coords) in enumerate(cards_pos):
                if j in assigned or j == i:
                    continue
                if window_start <= other_pos <= window_end:
                    group_indices.append(j)
                    group_cards.append(other_card)

            type_counter = Counter(_get_dtp_type(c) for c in group_cards)
            is_cluster, _ = _check_cluster_criteria(type_counter, len(group_cards))

            if is_cluster and _max_gps_spread(group_cards) <= NON_SETTLEMENT_GPS_MAX_SPREAD_M:
                assigned.update(group_indices)
                group_cards.sort(key=lambda c: _get_date(c))

                first_coords = _parse_coords(group_cards[0])
                last_coords = _parse_coords(group_cards[-1])

                # Если все ДТП в очаге — перекрёстки, помечаем как перекрёсток
                all_intersection = all(_is_intersection(c) for c in group_cards)
                zone = "nonsettlement_intersection" if all_intersection else "nonsettlement"

                all_clusters.append(
                    _build_cluster(
                        group_cards, coords or first_coords, zone,
                        road_name=road_name,
                        start_pos=window_start,
                        end_pos=window_end,
                    )
                )
            else:
                assigned.add(i)

    logger.info(f"Очаги вне НП: {len(all_clusters)} найдено")
    return all_clusters


# ========================
# Excel-выход
# ========================

ZONE_TYPE_LABELS = {
    "settlement_intersection": "НП - Перекрёсток",
    "settlement_road": "НП - Участок дороги (пикетаж)",
    "settlement_segment": "НП - Участок дороги",
    "nonsettlement_intersection": "Вне НП - Перекрёсток",
    "nonsettlement": "Вне НП",
}

CONCENTRATION_COLUMNS = [
    "№ очага",
    "Тип зоны",
    "Дорога/Улица",
    "Пикетаж начало",
    "Пикетаж конец",
    "Широта первого ДТП",
    "Долгота первого ДТП",
    "Широта последнего ДТП",
    "Долгота последнего ДТП",
    "Кол-во ДТП",
    "Виды ДТП (детализация)",
    "Доминирующий вид",
    "Погибло",
    "Ранено",
    "Дата первого ДТП",
    "Дата последнего ДТП",
    # --- Камеры фотовидеофиксации ---
    "Статус покрытия камерой",
    "Камера: номер",
    "Камера: адрес",
    "Камера: координаты",
    "Ближайшая камера: номер",
    "Ближайшая камера: адрес",
    "Ближайшая камера: координаты",
    "Расстояние до камеры (м)",
]

DETAIL_COLUMNS = [
    "№ очага",
    "Дата ДТП",
    "Вид ДТП",
    "Дорога/Улица",
    "Пикетаж",
    "Широта",
    "Долгота",
    "Погибло",
    "Ранено",
]

PRECLUSTER_COLUMNS = [
    "№ предочага",
    "Тип зоны",
    "Дорога/Улица",
    "Пикетаж начало",
    "Пикетаж конец",
    "Широта первого ДТП",
    "Долгота первого ДТП",
    "Широта последнего ДТП",
    "Долгота последнего ДТП",
    "Кол-во ДТП",
    "Виды ДТП (детализация)",
    "Доминирующий вид",
    "Погибло",
    "Ранено",
    "Дата первого ДТП",
    "Дата последнего ДТП",
    "Критерий предочага",
    # --- Камеры фотовидеофиксации ---
    "Статус покрытия камерой",
    "Камера: номер",
    "Камера: адрес",
    "Камера: координаты",
    "Ближайшая камера: номер",
    "Ближайшая камера: адрес",
    "Ближайшая камера: координаты",
    "Расстояние до камеры (м)",
]


def get_precluster_column_names() -> list[str]:
    """Названия колонок для Excel-файла предочагов."""
    return list(PRECLUSTER_COLUMNS)


