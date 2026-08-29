"""Карточные акцессоры — чистые функции извлечения данных из карточек ДТП."""
import math

from ._constants import (
    EARTH_RADIUS_KM,
    INTERSECTION_KEYWORDS,
    EXCLUDED_SDOR_ALWAYS, EXCLUDED_K_UL, EXCLUDED_SDOR_FOR_KUL,
)

def haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Расстояние в метрах между двумя точками по формуле Гаверсинуса."""
    lat1_r, lon1_r = math.radians(lat1), math.radians(lon1)
    lat2_r, lon2_r = math.radians(lat2), math.radians(lon2)
    dlat = lat2_r - lat1_r
    dlon = lon2_r - lon1_r
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlon / 2) ** 2
    )
    c = 2 * math.asin(math.sqrt(min(a, 1.0)))
    return EARTH_RADIUS_KM * c * 1000.0


def _max_gps_spread(cards: list[dict]) -> float:
    """Максимальное расстояние (м) между любой парой ДТП в группе по координатам.

    Защита от кольцевых дорог: если пикетаж обнуляется, ДТП с одинаковым
    пикетажем могут быть географически удалены. Возвращает 0 если координат
    меньше двух.
    """
    coords = [_parse_coords(c) for c in cards]
    coords = [c for c in coords if c is not None]
    if len(coords) < 2:
        return 0.0
    max_d = 0.0
    for i in range(len(coords)):
        for j in range(i + 1, len(coords)):
            d = haversine_meters(
                coords[i][0], coords[i][1],
                coords[j][0], coords[j][1],
            )
            if d > max_d:
                max_d = d
    return max_d


def _parse_coords(card: dict) -> tuple[float, float] | None:
    """Извлечь координаты из карточки. Возвращает (lat, lon) или None."""
    try:
        lat = float(str(card.get("coord_w", "")).strip())
        lon = float(str(card.get("coord_l", "")).strip())
        if lat != 0 and lon != 0:
            return (lat, lon)
    except (ValueError, TypeError):
        pass
    return None


def _is_intersection(card: dict) -> bool:
    """Является ли место ДТП перекрёстком (по полю sdor).

    Поле sdor содержит объект УДС на месте ДТП: перекрёсток,
    перегон, пешеходный переход и т.д.
    Данные лежат внутри card["dor_usl"]["sdor"] — это массив строк.
    """
    dor_usl = card.get("dor_usl") or {}
    sdor_list = dor_usl.get("sdor") or []
    if isinstance(sdor_list, list):
        for item in sdor_list:
            item_lower = str(item).strip().lower()
            for keyword in INTERSECTION_KEYWORDS:
                if keyword in item_lower:
                    return True
    return False


def _is_off_road(card: dict) -> bool:
    """Произошло ли ДТП вне дороги (внутридворовая территория, автостоянка).

    Такие ДТП не могут входить в очаги аварийности.
    Двойная проверка:
    1. sdor содержит «внутридворовая территория» или «отделенная от проезжей части»
       → всегда исключается
    2. k_ul == «Иные места» И sdor содержит «Выезд с прилегающей территории»,
       «Тротуар, пешеходная дорожка» или «Иное место» → исключается
    """
    dor_usl = card.get("dor_usl") or {}
    sdor_list = dor_usl.get("sdor") or []
    sdor_lower = []
    if isinstance(sdor_list, list):
        sdor_lower = [str(item).strip().lower() for item in sdor_list]

    # 1) Всегда исключаем по sdor
    for item_lower in sdor_lower:
        for keyword in EXCLUDED_SDOR_ALWAYS:
            if keyword in item_lower:
                return True

    # 2) Исключаем по k_ul + sdor
    k_ul = str(card.get("k_ul", "")).strip().lower()
    if k_ul == EXCLUDED_K_UL:
        for item_lower in sdor_lower:
            for keyword in EXCLUDED_SDOR_FOR_KUL:
                if keyword in item_lower:
                    return True

    return False


def _get_dtp_type(card: dict) -> str:
    """Вид ДТП."""
    return str(card.get("dtpv", "")).strip()


def _get_road_name(card: dict) -> str:
    """Название дороги/улицы."""
    dor = str(card.get("dor", "")).strip()
    if dor:
        return dor
    return str(card.get("street", "")).strip()


def _get_date(card: dict) -> str:
    """Дата ДТП."""
    return str(card.get("date_dtp", "")).strip()


def _get_km_m(card: dict) -> float | None:
    """
    Пикетаж как float (км.ddd). km=12, m=500 -> 12.500

    Возвращает None если:
      - поле km пустое
      - оба значения равны 0 (0+000 = «не указан»)
    """
    km_str = str(card.get("km", "")).strip()
    m_str = str(card.get("m", "")).strip()
    if km_str:
        try:
            km_val = float(km_str)
            m_val = float(m_str) if m_str else 0.0
            total = km_val + m_val / 1000.0
            # 0+000 означает «пикетаж не указан»
            if total == 0.0:
                return None
            return total
        except ValueError:
            pass
    return None


def _has_road_and_piketazh(card: dict) -> bool:
    """Есть ли у карточки наименование дороги И пикетаж."""
    return bool(_get_road_name(card)) and _get_km_m(card) is not None


