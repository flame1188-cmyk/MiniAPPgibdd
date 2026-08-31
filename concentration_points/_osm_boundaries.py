"""OSM/Overpass: кэш, HTTP-запросы, парсинг полигонов, classify_cards."""
import json
import os
import time
import gc
import hashlib
import logging
from collections import OrderedDict
from typing import Callable, Awaitable
import asyncio

import httpx
from shapely.geometry import Polygon, MultiPolygon, Point, LineString
from shapely.ops import linemerge, polygonize, unary_union
from shapely.prepared import prep
from shapely.strtree import STRtree

from analytics import _safe_int

# --- Загрузка полигонов из PostgreSQL (если доступна БД) ---
def _try_load_polygons_from_db(reg_code: str):
    """Пытается загрузить полигоны региона из БД (синхронная обёртка).

    Используется в fetch_settlement_boundaries как приоритетный источник.
    Возвращает list[Polygon|MultiPolygon] или None.
    """
    try:
        from miniapp.backend.db.connection import get_pool
        from miniapp.backend.db.polygon_repository import load_polygons_from_db
        pool = get_pool()
        if pool is None:
            return None
        # Синхронная обёртка над async-функцией
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Внутри async-контекста — используем create_task
            return None  # Фолбэк на JSON-кэш, DB будет проверена в async-контексте
        return loop.run_until_complete(load_polygons_from_db(pool, reg_code))
    except Exception as e:
        logger.debug(f"DB polygons not available for {reg_code}: {e}")
        return None


async def _load_polygons_from_db_async(reg_code: str):
    """Async-загрузка полигонов региона из PostgreSQL.

    Возвращает list[Polygon|MultiPolygon] или None.
    """
    try:
        from miniapp.backend.db.connection import get_pool
        from miniapp.backend.db.polygon_repository import load_polygons_from_db
        pool = get_pool()
        if pool is None:
            return None
        return await load_polygons_from_db(pool, reg_code)
    except Exception as e:
        logger.debug(f"DB polygons not available for {reg_code}: {e}")
        return None


from ._constants import (
    CACHE_DIR, REGION_CACHE_DIR, CACHE_TTL_SECONDS, REGION_CACHE_TTL_SECONDS,
    MEMORY_CACHE_MAX, BBOX_MARGIN, BBOX_TILE_MAX_DEG, BBOX_TILE_OVERLAP,
    BBOX_MIN_CLAMP, UNARY_UNION_MAX_POLYGONS, POLYGON_SIMPLIFY_TOLERANCE,
    OVERPASS_URLS, OVERPASS_HEADERS, OVERPASS_MIN_INTERVAL, OVERPASS_429_WAIT,
    OVERPASS_REQUEST_TIMEOUT, PLACE_FILTER,
)
from ._card_accessors import _parse_coords

logger = logging.getLogger(__name__)

# Глобальное состояние
_memory_cache: OrderedDict[str, tuple[float, list]] = OrderedDict()
_overpass_client: httpx.AsyncClient | None = None
_overpass_last_request_time: float = 0.0

def _memory_cache_get(bbox_str: str) -> list[Polygon | MultiPolygon] | None:
    """Получить полигоны из in-memory кэша. Returns None если нет или просрочен."""
    if bbox_str in _memory_cache:
        ts, polygons = _memory_cache[bbox_str]
        age = time.time() - ts
        if age < CACHE_TTL_SECONDS:
            # Перемещаем в конец (LRU)
            _memory_cache.move_to_end(bbox_str)
            logger.info(
                f"In-memory кэш границ НП: hit (возраст {age / 3600:.1f} ч, "
                f"{len(polygons)} полигонов)"
            )
            return polygons
        else:
            del _memory_cache[bbox_str]
    return None


def _memory_cache_put(bbox_str: str, polygons: list[Polygon | MultiPolygon]) -> None:
    """Сохранить полигоны в in-memory LRU кэш."""
    while len(_memory_cache) >= MEMORY_CACHE_MAX:
        _memory_cache.popitem(last=False)  # удаляем самый старый
    _memory_cache[bbox_str] = (time.time(), polygons)
    logger.info(
        f"In-memory кэш границ НП: сохранено ({len(polygons)} полигонов, "
        f"LRU размер: {len(_memory_cache)}/{MEMORY_CACHE_MAX})"
    )


# ========================
# Дисковый кэш элементов Overpass
# ========================

def _cache_path(bbox_str: str) -> str:
    """Путь к файлу кэша для данного BBOX."""
    h = hashlib.md5(bbox_str.encode()).hexdigest()[:12]
    return os.path.join(CACHE_DIR, f"settlements_{h}.json")


def _load_cache(bbox_str: str) -> list[dict] | None:
    """
    Загружает кэшированный ответ Overpass API.

    Returns:
        Список elements из Overpass или None, если кэш отсутствует/просрочен.
    """
    path = _cache_path(bbox_str)
    if not os.path.exists(path):
        return None

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        age = time.time() - data.get("timestamp", 0)
        if age > CACHE_TTL_SECONDS:
            logger.info(
                f"Кэш границ НП просрочен: {path} "
                f"(возраст: {age / 3600:.1f} ч)"
            )
            return None

        logger.info(
            f"Кэш границ НП загружен: {path} "
            f"(возраст: {age / 3600:.1f} ч, "
            f"{data.get('count', 0)} элементов)"
        )
        return data.get("elements", [])
    except Exception as e:
        logger.warning(f"Ошибка чтения кэша: {e}")
        return None


def _save_cache(bbox_str: str, elements: list[dict]) -> None:
    """Сохраняет ответ Overpass API в кэш."""
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        path = _cache_path(bbox_str)
        data = {
            "timestamp": time.time(),
            "bbox": bbox_str,
            "count": len(elements),
            "elements": elements,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        logger.info(
            f"Кэш границ НП сохранён: {path} "
            f"({len(elements)} элементов)"
        )
    except Exception as e:
        logger.warning(f"Ошибка записи кэша: {e}")


# ========================
# Регион-уровень кэша (для precache_osm.py)
# ========================

def _region_cache_path(reg_code: str) -> str:
    """Путь к файлу кэша для региона по его коду."""
    # Безопасное имя файла: только цифры (код региона ГИБДД — 4 цифры).
    safe = "".join(c for c in str(reg_code) if c.isdigit()) or "unknown"
    return os.path.join(REGION_CACHE_DIR, f"region_{safe}.json")


def _load_region_cache(reg_code: str) -> list[dict] | None:
    """
    Загружает предкэшированные элементы Overpass для целого региона.

    Returns:
        Список elements из Overpass или None, если кэш отсутствует/просрочен.
    """
    path = _region_cache_path(reg_code)
    if not os.path.exists(path):
        return None

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        age = time.time() - data.get("timestamp", 0)
        ttl = data.get("ttl_seconds", REGION_CACHE_TTL_SECONDS)
        if age > ttl:
            logger.info(
                f"Региональный кэш границ НП просрочен: {path} "
                f"(возраст: {age / 86400:.1f} дней)"
            )
            return None

        elements = data.get("elements", [])
        logger.info(
            f"Региональный кэш границ НП загружен: {path} "
            f"(возраст: {age / 86400:.1f} дней, "
            f"{len(elements)} элементов)"
        )
        return elements
    except Exception as e:
        logger.warning(f"Ошибка чтения регионального кэша ({reg_code}): {e}")
        return None


def _save_region_cache(reg_code: str, elements: list[dict], region_name: str = "") -> None:
    """
    Сохраняет элементы Overpass для региона в кэш.

    Используется скриптом precache_osm.py для прогрева кэша топ-N регионов.
    Формат файла идентичен _save_cache() плюс поля region_code/region_name.
    """
    try:
        os.makedirs(REGION_CACHE_DIR, exist_ok=True)
        path = _region_cache_path(reg_code)
        data = {
            "timestamp": time.time(),
            "ttl_seconds": REGION_CACHE_TTL_SECONDS,
            "region_code": str(reg_code),
            "region_name": region_name,
            "count": len(elements),
            "elements": elements,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        logger.info(
            f"Региональный кэш границ НП сохранён: {path} "
            f"({len(elements)} элементов, регион: {region_name or reg_code})"
        )
    except Exception as e:
        logger.warning(f"Ошибка записи регионального кэша ({reg_code}): {e}")


# ========================
# OSM: Разбор полигонов
# ========================

def _way_to_polygon(element: dict) -> Polygon | None:
    """
    Преобразует way-элемент Overpass (out geom) в Shapely Polygon.

    Shapely использует (x, y) = (lon, lat), поэтому координаты
    переставляются при создании полигона.
    """
    geom = element.get("geometry", [])
    if len(geom) < 4:
        return None

    try:
        coords = [(n["lon"], n["lat"]) for n in geom]
        poly = Polygon(coords)
        if not poly.is_valid:
            poly = poly.buffer(0)
        if poly.is_empty or poly.area < 1e-10:
            return None
        return poly
    except Exception:
        return None


def _relation_to_polygon(
    element: dict,
) -> Polygon | MultiPolygon | None:
    """
    Преобразует relation-элемент Overpass (out geom) в Shapely Polygon.

    Алгоритм:
    1. Собирает outer-кольца из member-ов (role=outer или без роли)
    2. Объединяет через linemerge → замкнутые кольца
    3. polygonize → список Polygon
    4. Inner-кольца (role=inner) вычитаются как отверстия (holes)
    """
    members = element.get("members", [])
    if not members:
        return None

    outer_rings: list[list[tuple[float, float]]] = []
    inner_rings: list[list[tuple[float, float]]] = []

    for member in members:
        geom = member.get("geometry", [])
        if len(geom) < 2:
            continue

        coords = [(n["lon"], n["lat"]) for n in geom]
        role = member.get("role", "outer")

        if role == "inner":
            inner_rings.append(coords)
        else:
            outer_rings.append(coords)

    if not outer_rings:
        return None

    try:
        outer_lines = [LineString(ring) for ring in outer_rings]
        merged = linemerge(outer_lines)

        polygons: list[Polygon] = []

        if merged.geom_type == "LineString":
            if merged.is_closed:
                polygons.append(Polygon(merged))
        elif merged.geom_type == "MultiLineString":
            polygons.extend(polygonize(merged))
        else:
            return None

        if not polygons:
            return None

        # Обработка отверстий (inner-кольца)
        if inner_rings:
            for i, poly in enumerate(polygons):
                for hole_coords in inner_rings:
                    try:
                        hole_line = LineString(hole_coords)
                        if hole_line.is_closed and poly.contains(hole_line):
                            hole_poly = Polygon(hole_coords)
                            polygons[i] = poly.difference(hole_poly)
                    except Exception:
                        pass

        # Валидация
        valid_polygons: list[Polygon] = []
        for p in polygons:
            if not p.is_valid:
                p = p.buffer(0)
            if not p.is_empty and p.area > 1e-10:
                valid_polygons.append(p)

        if not valid_polygons:
            return None

        if len(valid_polygons) == 1:
            return valid_polygons[0]
        return MultiPolygon(valid_polygons)

    except Exception as e:
        logger.debug(
            f"Не удалось разобрать relation id={element.get('id')}: {e}"
        )
        return None


def _parse_overpass_elements(
    elements: list[dict],
) -> tuple[list[Polygon | MultiPolygon], bool]:
    """
    Преобразует элементы Overpass API в список Shapely-полигонов.

    Поддерживает два формата ответа:
    - «out geom»: поля geometry (ways) / members (relations)
    - «out bb»: поля bounds (прямоугольные оболочки)

    Приоритет: geom > bb. Если geom-данные есть — используются они,
    если нет — падаем обратно на bounding boxes (совместимость).

    Returns:
        (polygons, is_bbox_fallback) — список полигонов и флаг,
        что использовались bounding boxes (а не реальные полигоны).
    """
    return _parse_overpass_elements_with_ids(elements)[:2]


def _parse_overpass_elements_with_ids(
    elements: list[dict],
) -> tuple[list[Polygon | MultiPolygon], bool, list[tuple[str, int]]]:
    """
    Как _parse_overpass_elements, но также возвращает список OSM-ID
    каждого полигона: [(type, id), ...]. Нужен для инкрементальной
    дедупликации по тайлам без хранения сырых JSON-элементов.

    Returns:
        (polygons, is_bbox_fallback, element_ids)
    """
    polygons: list[Polygon | MultiPolygon] = []
    element_ids: list[tuple[str, int]] = []

    # Первый проход: проверяем, есть ли geom-данные
    has_geom = False
    for element in elements:
        if element.get("type") == "way" and element.get("geometry"):
            has_geom = True
        elif element.get("type") == "relation" and element.get("members"):
            has_geom = True

    if has_geom:
        for element in elements:
            el_type = element.get("type", "")
            el_id = element.get("id", 0)
            poly = None
            if el_type == "way":
                poly = _way_to_polygon(element)
            elif el_type == "relation":
                poly = _relation_to_polygon(element)
            if poly is not None:
                polygons.append(poly)
                element_ids.append((el_type, el_id))

        # Упрощаем полигоны для экономии памяти.
        # Для задачи «точка в НП / вне НП» точность 22 м избыточна.
        if len(polygons) > UNARY_UNION_MAX_POLYGONS:
            simplified = []
            simplified_ids = []
            for p, eid in zip(polygons, element_ids):
                try:
                    s = p.simplify(POLYGON_SIMPLIFY_TOLERANCE, preserve_topology=True)
                    if not s.is_empty and s.area > 1e-10:
                        simplified.append(s)
                        simplified_ids.append(eid)
                except Exception:
                    simplified.append(p)
                    simplified_ids.append(eid)
            before = len(polygons)
            polygons = simplified
            element_ids = simplified_ids
            logger.info(
                f"Полигоны упрощены: {before} → {len(polygons)} "
                f"(допуск {POLYGON_SIMPLIFY_TOLERANCE}°)")

    if polygons:
        logger.info(
            f"Разобрано {len(polygons)} полигонов НП из OSM (out geom)"
        )
        return polygons, False, element_ids

    # Fallback: bounding boxes (out bb)
    for element in elements:
        if "bounds" in element:
            b = element["bounds"]
            coords = [
                (b["minlon"], b["minlat"]),
                (b["maxlon"], b["minlat"]),
                (b["maxlon"], b["maxlat"]),
                (b["minlon"], b["maxlat"]),
            ]
            try:
                poly = Polygon(coords)
                if poly.is_valid and poly.area > 0:
                    polygons.append(poly)
                    element_ids.append((
                        element.get("type", ""), element.get("id", 0),
                    ))
            except Exception:
                pass

    logger.info(
        f"Разобрано {len(polygons)} bounding boxes из OSM "
        f"(out bb fallback)"
    )
    return polygons, True, element_ids


# ========================
# OSM: Определение границ НП
# ========================

# ========================
# Bbox утилиты
# ========================

def _compute_bbox_tiles(
    lat_min: float, lon_min: float,
    lat_max: float, lon_max: float,
) -> list[tuple[float, float, float, float]]:
    """
    Разбивает большой bbox на тайлы, если любая сторона > BBOX_TILE_MAX_DEG.

    Возвращает список (lat_min, lon_min, lat_max, lon_max) тайлов.
    Тайлы имеют перехлёст BBOX_TILE_OVERLAP, чтобы НП на границах не терялись.
    """
    lat_span = lat_max - lat_min
    lon_span = lon_max - lon_min

    if lat_span <= BBOX_TILE_MAX_DEG and lon_span <= BBOX_TILE_MAX_DEG:
        # Достаточно маленький — не разбиваем
        return [(lat_min, lon_min, lat_max, lon_max)]

    tiles: list[tuple[float, float, float, float]] = []
    overlap = BBOX_TILE_OVERLAP

    # Разбиваем по широте
    lat_steps = max(1, math.ceil(lat_span / BBOX_TILE_MAX_DEG))
    # Разбиваем по долготе
    lon_steps = max(1, math.ceil(lon_span / BBOX_TILE_MAX_DEG))

    for li in range(lat_steps):
        for lj in range(lon_steps):
            t_lat_min = lat_min + li * lat_span / lat_steps - overlap
            t_lat_max = lat_min + (li + 1) * lat_span / lat_steps + overlap
            t_lon_min = lon_min + lj * lon_span / lon_steps - overlap
            t_lon_max = lon_min + (lj + 1) * lon_span / lon_steps + overlap

            # Ограничиваем мировыми границами
            t_lat_min = max(t_lat_min, 41.0)
            t_lat_max = min(t_lat_max, 70.0)
            t_lon_min = max(t_lon_min, 19.0)
            t_lon_max = min(t_lon_max, 180.0)

            tiles.append((t_lat_min, t_lon_min, t_lat_max, t_lon_max))

    logger.info(
        f"Bbox разбит на {len(tiles)} тайлов "
        f"({lat_steps}x{lon_steps}, span: {lat_span:.2f}x{lon_span:.2f}°)"
    )
    return tiles


def _dedup_elements(elements: list[dict]) -> list[dict]:
    """
    Удаляет дубликаты элементов Overpass по (type, id).
    Нужно при слиянии результатов из нескольких тайлов.
    """
    seen: set[tuple[str, int]] = set()
    unique = []
    for el in elements:
        key = (el.get("type", ""), el.get("id", 0))
        if key not in seen:
            seen.add(key)
            unique.append(el)
    if len(unique) < len(elements):
        logger.info(
            f"Дедупликация элементов: {len(elements)} → {len(unique)} "
            f"(удалено {len(elements) - len(unique)} дублей)"
        )
    return unique


# ========================
# OSM: Определение границ НП
# ========================

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
    "https://z.overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

OVERPASS_HEADERS = {
    "User-Agent": "GIBDD-DTP-Bot/1.0 (traffic-accident-analysis)",
    "Accept": "application/json",
}

# Rate limiting для Overpass API
# Phase C.4: уменьшено с 10.0 до 5.0 сек.
# При недоступности Overpass (504 на всех зеркалах) прежние 10 сек sleep
# означали 4 зеркала × 10 сек = 40 сек бесполезного ожидания. 5 сек всё
# ещё достаточно для соблюдения rate limit (Overpass рекомендует ≥5 сек
# между запросами от одного User-Agent).
OVERPASS_MIN_INTERVAL = 5.0  # мин. интервал между запросами (сек)
OVERPASS_429_WAIT = 30.0     # базовое ожидание при 429 (сек)
_overpass_client: httpx.AsyncClient | None = None  # общий HTTP-клиент
_overpass_last_request_time: float = 0.0  # время последнего запроса

# Phase C.4: таймаут на один запрос к Overpass.
# Раньше было 60 сек (унаследовано из дефолта httpx). При 504 (gateway
# timeout у самого Overpass) это не помогает — 504 приходит за ~300 мс.
# Но при сетевых лагах 60 сек слишком долго: 4 зеркала × 60 сек = 4 мин
# бесполезного ожидания. 30 сек — баланс между «реальный медленный, но
# отвечающий Overpass» и быстрым failover.
OVERPASS_REQUEST_TIMEOUT = 30.0

PLACE_FILTER = "city|town|village|hamlet"


async def fetch_settlement_boundaries(
    cards: list[dict],
    progress_callback: Callable[[str], Awaitable[None]] | None = None,
    reg_code: str | None = None,
) -> list[Polygon | MultiPolygon]:
    """
    Получает полигоны границ населённых пунктов через Overpass API.

    Оптимизации памяти (особенно для крупных регионов):
    1. Инкрементальная обработка тайлов — каждый тайл парсится
       в полигоны сразу, сырой JSON удаляется до загрузки следующего.
    2. In-memory LRU-кэш — избегает повторного парсинга JSON.
    3. STRtree вместо unary_union при >2000 полигонов.
    4. Упрощение полигонов (preserve_topology).

    Приоритеты кэша:
    1. In-memory LRU (по bbox ИЛИ по "region:{code}")
    2. Регион-уровневый кэш (если передан reg_code) — для предкэшированных
       топ-N регионов. Содержит ВСЕ НП региона за один раз.
    3. Тайл-уровневый кэш + live-запрос к Overpass (как раньше)

    Args:
        cards: Список карточек ДТП (используется для вычисления bbox)
        progress_callback: async-функция для обновления статуса
        reg_code: Код региона ГИБДД (например, "1145" для Москвы).
            Если передан — сначала проверяется регион-уровневый кэш.

    Returns:
        Список Shapely-полигонов (Polygon или MultiPolygon).
    """
    valid_coords = [_parse_coords(c) for c in cards]
    valid_coords = [c for c in valid_coords if c is not None]

    if not valid_coords:
        return []

    lats = [c[0] for c in valid_coords]
    lons = [c[1] for c in valid_coords]

    # Адаптивный bbox: мин. запас 0.02° (~2.2 км) вокруг крайних ДТП
    raw_lat_min = min(lats) - BBOX_MARGIN
    raw_lon_min = min(lons) - BBOX_MARGIN
    raw_lat_max = max(lats) + BBOX_MARGIN
    raw_lon_max = max(lons) + BBOX_MARGIN

    # Ограничиваем минимальный размер bbox
    if raw_lat_max - raw_lat_min < BBOX_MIN_CLAMP:
        mid_lat = (raw_lat_max + raw_lat_min) / 2
        raw_lat_min = mid_lat - BBOX_MIN_CLAMP / 2
        raw_lat_max = mid_lat + BBOX_MIN_CLAMP / 2
    if raw_lon_max - raw_lon_min < BBOX_MIN_CLAMP:
        mid_lon = (raw_lon_max + raw_lon_min) / 2
        raw_lon_min = mid_lon - BBOX_MIN_CLAMP / 2
        raw_lon_max = mid_lon + BBOX_MIN_CLAMP / 2

    # Clamp к мировым границам
    lat_min = max(raw_lat_min, 41.0)
    lon_min = max(raw_lon_min, 19.0)
    lat_max = min(raw_lat_max, 70.0)
    lon_max = min(raw_lon_max, 180.0)

    bbox = f"{lat_min},{lon_min},{lat_max},{lon_max}"

    # --- Шаг 0: PostgreSQL (приоритет) ---
    # Если полигоны загружены в БД через precache_osm.py --db
    # или через API /api/polygons/{region_code}/import — используем их.
    if reg_code:
        region_key = f"region:{reg_code}"
        mem_polygons = _memory_cache_get(region_key)
        if mem_polygons is not None:
            logger.info(
                f"Используем регион-кэш (in-memory) для {reg_code}: "
                f"{len(mem_polygons)} полигонов"
            )
            return mem_polygons

        # Пробуем загрузить из PostgreSQL
        db_polygons = await _load_polygons_from_db_async(reg_code)
        if db_polygons is not None:
            _memory_cache_put(region_key, db_polygons)
            logger.info(
                f"Полигоны региона {reg_code} из БД: "
                f"{len(db_polygons)} полигонов (OSM-запрос не требуется)"
            )
            return db_polygons

        # Дисковый регион-кэш (фолбэк)
        region_elements = _load_region_cache(reg_code)
        if region_elements is not None:
            if progress_callback:
                await progress_callback(
                    f"Загрузка границ НП из кэша региона {reg_code}..."
                )
            region_polys, region_is_bbox, _ = _parse_overpass_elements_with_ids(
                region_elements
            )
            if region_polys and not region_is_bbox:
                # Сохраняем в in-memory LRU (полный набор региона).
                # Фильтрация по bbox текущих ДТП произойдёт позже
                # в classify_dtp_by_settlements (STRtree выполнит
                # эффективный пространственный запрос).
                _memory_cache_put(region_key, region_polys)
                logger.info(
                    f"Региональный кэш {reg_code}: "
                    f"загружено {len(region_polys)} полигонов, "
                    f"OSM-запрос не требуется"
                )
                del region_elements
                return region_polys
            else:
                logger.warning(
                    f"Региональный кэш {reg_code}: содержит bbox-фолбэк, "
                    f"игнорируем и делаем тайл-запрос"
                )
            del region_elements

    # --- Шаг 1: In-memory кэш по bbox (как раньше) ---
    mem_polygons = _memory_cache_get(bbox)
    if mem_polygons is not None:
        return mem_polygons

    # --- Шаг 2: Разбиваем на тайлы ---
    tiles = _compute_bbox_tiles(lat_min, lon_min, lat_max, lon_max)

    if progress_callback:
        tile_info = f" ({len(tiles)} тайлов)" if len(tiles) > 1 else ""
        await progress_callback(
            f"Загрузка границ НП из OpenStreetMap{tile_info}...\n"
            f"BBOX: {bbox}"
        )

    # --- Шаг 3: Инкрементальная обработка тайлов ---
    # Каждым тайлом: загрузка → парсинг → освобождение JSON → gc.
    # НЕ копим сырые JSON-элементы в all_elements!
    all_polygons: list[Polygon | MultiPolygon] = []
    seen_ids: set[tuple[str, int]] = set()
    any_bbox = False  # был ли хотя бы один bbox fallback
    total_elements_processed = 0

    for tile_idx, (t_lat_min, t_lon_min, t_lat_max, t_lon_max) in enumerate(tiles):
        tile_bbox = f"{t_lat_min},{t_lon_min},{t_lat_max},{t_lon_max}"

        # Проверяем дисковый кэш для тайла
        cached_elements = _load_cache(tile_bbox)
        if cached_elements is not None:
            tile_polys, tile_is_bbox, tile_ids = (
                _parse_overpass_elements_with_ids(cached_elements)
            )
            if tile_polys and not tile_is_bbox:
                # Дедуплицируем по OSM ID
                new_polys, new_ids = _dedup_polygons_by_id(
                    tile_polys, tile_ids, seen_ids,
                )
                all_polygons.extend(new_polys)
                total_elements_processed += len(cached_elements)
                logger.info(
                    f"Тайл {tile_idx + 1}/{len(tiles)}: из дискового кэша "
                    f"({len(cached_elements)} элементов, "
                    f"{len(new_polys)} новых полигонов)"
                )
                del cached_elements
                gc.collect()
                continue
            elif tile_polys and tile_is_bbox:
                logger.info(
                    f"Тайл {tile_idx + 1}/{len(tiles)}: кэш содержит bbox, "
                    f"запрашиваем заново"
                )
            else:
                logger.info(
                    f"Тайл {tile_idx + 1}/{len(tiles)}: кэш пуст, "
                    f"запрашиваем OSM"
                )

        # Запрос к Overpass (используем адаптивный place_filter)
        elements = await _fetch_overpass_parallel(
            tile_bbox, tile_idx, len(tiles),
            place_filter=PLACE_FILTER,
        )
        if elements:
            # Сразу парсим в полигоны и освобождаем JSON
            tile_polys, tile_is_bbox, tile_ids = (
                _parse_overpass_elements_with_ids(elements)
            )
            total_elements_processed += len(elements)

            if tile_is_bbox:
                any_bbox = True
                logger.info(
                    f"Тайл {tile_idx + 1}/{len(tiles)}: получен bbox "
                    f"({len(elements)} элементов)"
                )

            # Дедуплицируем и добавляем
            new_polys, new_ids = _dedup_polygons_by_id(
                tile_polys, tile_ids, seen_ids,
            )
            all_polygons.extend(new_polys)

            logger.info(
                f"Тайл {tile_idx + 1}/{len(tiles)}: "
                f"{len(tile_polys)} полигонов, "
                f"{len(new_polys)} новых (всего: {len(all_polygons)})"
            )

            # Кэшируем на диск (сырые элементы для конкретного тайла)
            if not tile_is_bbox:
                _save_cache(tile_bbox, elements)

            # Освобождаем память от сырого JSON и парсинга
            del elements
            del tile_polys
            del tile_ids

        if progress_callback:
            await progress_callback(
                f"Загрузка границ НП: {tile_idx + 1}/{len(tiles)} тайлов "
                f"({len(all_polygons)} полигонов)..."
            )

    if not all_polygons:
        logger.error(
            "Все зеркала Overpass API недоступны. "
            "Не удалось получить границы НП."
        )
        return []

    # --- Шаг 4: Сохраняем в кэши ---
    if not any_bbox:
        _memory_cache_put(bbox, all_polygons)
    else:
        logger.warning(
            "Некоторые тайлы вернули bounding boxes — "
            "результат НЕ кэширован в памяти"
        )

    logger.info(
        f"Итого границ НП: {len(all_polygons)} полигонов "
        f"(элементов обработано: {total_elements_processed}, "
        f"тайлов: {len(tiles)})"
    )
    return all_polygons


def _dedup_polygons_by_id(
    polygons: list[Polygon | MultiPolygon],
    element_ids: list[tuple[str, int]],
    seen_ids: set[tuple[str, int]],
) -> tuple[list[Polygon | MultiPolygon], list[tuple[str, int]]]:
    """
    Фильтрует полигоны, оставляя только те, чей OSM ID
    ещё не встречался. Обновляет seen_ids на месте.
    """
    new_polys = []
    new_ids = []
    for poly, eid in zip(polygons, element_ids):
        if eid not in seen_ids:
            seen_ids.add(eid)
            new_polys.append(poly)
            new_ids.append(eid)
    dupes = len(polygons) - len(new_polys)
    if dupes > 0:
        logger.debug(f"Дедупликация: удалено {dupes} дублей")
    return new_polys, new_ids


def _filter_elements_for_bbox(
    elements: list[dict],
    lat_min: float, lon_min: float,
    lat_max: float, lon_max: float,
) -> list[dict]:
    """
    Фильтрует элементы Overpass, оставляя только те, чей центр
    попадает в указанный bbox. Используется при кэшировании по тайлам.
    """
    filtered = []
    for el in elements:
        bounds = el.get("bounds")
        if bounds:
            center_lat = (bounds.get("minlat", 0) + bounds.get("maxlat", 0)) / 2
            center_lon = (bounds.get("minlon", 0) + bounds.get("maxlon", 0)) / 2
            if lat_min <= center_lat <= lat_max and lon_min <= center_lon <= lon_max:
                filtered.append(el)
            continue
        # Для элементов с geometry (out geom) — по первой координате
        geom = el.get("geometry") or []
        members = el.get("members") or []
        if geom:
            ref = geom[0]
            ref_lat = ref.get("lat", 0)
            ref_lon = ref.get("lon", 0)
            if lat_min <= ref_lat <= lat_max and lon_min <= ref_lon <= lon_max:
                filtered.append(el)
        elif members:
            for m in members:
                m_geom = m.get("geometry", [])
                if m_geom:
                    ref = m_geom[0]
                    ref_lat = ref.get("lat", 0)
                    ref_lon = ref.get("lon", 0)
                    if lat_min <= ref_lat <= lat_max and lon_min <= ref_lon <= lon_max:
                        filtered.append(el)
                        break
    return filtered


def _get_overpass_client() -> httpx.AsyncClient:
    """Возвращает общий httpx.AsyncClient для запросов к Overpass."""
    global _overpass_client
    if _overpass_client is None or _overpass_client.is_closed:
        _overpass_client = httpx.AsyncClient(
            verify=False,
            headers=OVERPASS_HEADERS,
            timeout=OVERPASS_REQUEST_TIMEOUT,  # Phase C.4: 30 сек вместо 60
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        )
    return _overpass_client


async def close_overpass_client() -> None:
    """Закрывает общий HTTP-клиент Overpass. Вызывается из bot.py при shutdown."""
    global _overpass_client
    if _overpass_client is not None and not _overpass_client.is_closed:
        await _overpass_client.aclose()
        _overpass_client = None


async def _overpass_request(
    url: str,
    query: str,
    mode: str,
) -> tuple[list[dict] | None, int | None]:
    """
    Выполняет единичный запрос к Overpass API с rate limiting.

    Rate limiting:
    - Между любыми запросами — мин. OVERPASS_MIN_INTERVAL секунд
    - При HTTP 429 — пауза Retry-After или OVERPASS_429_WAIT
    - Использует общий httpx.AsyncClient (connection pooling)

    Phase C.4: возвращает tuple (elements, status_code).
    - elements = list[dict] | None: распарсенные OSM elements (или None при ошибке)
    - status_code = int | None: HTTP-статус ответа (для диагностики failover).
      None — если сетевая ошибка / таймаут (не HTTP-ответ).

    Это позволяет _fetch_overpass_parallel отличать:
    - 504 от всех зеркал → сразу поднять RuntimeError (быстрый failover)
    - 429 от всех зеркал → подождать и retry
    - None (сетевая ошибка) → попробовать следующее зеркало
    """
    global _overpass_last_request_time

    # Rate limiting: ждём, если предыдущий запрос был недавно
    now = time.time()
    elapsed = now - _overpass_last_request_time
    if elapsed < OVERPASS_MIN_INTERVAL:
        wait = OVERPASS_MIN_INTERVAL - elapsed
        logger.debug(f"Overpass rate limit: ждём {wait:.1f}с...")
        await asyncio.sleep(wait)

    try:
        logger.info(
            f"Overpass API ({url}): запрос (mode={mode})..."
        )
        _overpass_last_request_time = time.time()

        client = _get_overpass_client()
        resp = await client.post(url, data={"data": query}, timeout=OVERPASS_REQUEST_TIMEOUT)

        if resp.status_code == 429:
            # Парсим Retry-After
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                try:
                    wait_sec = float(retry_after)
                except ValueError:
                    wait_sec = OVERPASS_429_WAIT
            else:
                wait_sec = OVERPASS_429_WAIT
            logger.warning(
                f"Overpass API ({url}, mode={mode}): HTTP 429, "
                f"ждём {wait_sec:.0f}с (Retry-After: {retry_after or 'нет'})"
            )
            await asyncio.sleep(wait_sec)
            # Повторный запрос после ожидания
            _overpass_last_request_time = time.time()
            resp = await client.post(url, data={"data": query}, timeout=OVERPASS_REQUEST_TIMEOUT)

        if resp.status_code >= 400:
            # Любая HTTP-ошибка (включая 504, 502, 503, 429) — логируем
            # и возвращаем статус для failover-логики.
            logger.warning(
                f"Overpass API ({url}, mode={mode}): "
                f"HTTP {resp.status_code}"
            )
            return None, resp.status_code

        resp.raise_for_status()
        data = resp.json()

        elements = data.get("elements", [])
        logger.info(
            f"Overpass API ({url}): получено "
            f"{len(elements)} элементов (mode={mode})"
        )
        return elements, 200

    except httpx.HTTPStatusError as e:
        logger.warning(
            f"Overpass API ({url}, mode={mode}): "
            f"HTTP {e.response.status_code}"
        )
        return None, e.response.status_code
    except Exception as e:
        # Сетевая ошибка / таймаут / парсинг JSON — нет HTTP-статуса.
        logger.warning(
            f"Overpass API ({url}, mode={mode}): {e}"
        )
        return None, None


async def _fetch_overpass_parallel(
    bbox_str: str,
    tile_idx: int = 0,
    total_tiles: int = 1,
    place_filter: str | None = None,
) -> list[dict] | None:
    """
    Последовательный запрос к зеркалам Overpass API с rate limiting.

    Стратегия:
    1. Последовательно пробуем все зеркала — out geom (1 проход)
       Phase C.4: раньше было 2 прохода, но если все зеркала дали 504/429,
       второй проход бесполезен — Overpass лежит глобально.
    2. Fallback: последовательно — out bb на всех зеркалах
       (только если geom вернул EmptyElements, а не 504 — иначе нет смысла)
    3. Rate limiter обеспечивает мин. 5 сек между запросами

    Phase C.4: БЫСТРЫЙ FAILOVER при 504/502/503
    - Если ВСЕ зеркала на первом проходе вернули 5xx (gateway errors) —
      поднимаем RuntimeError. Раньше цикл шёл дальше: второй проход +
      bb-fallback = ~30 сек бесполезного ожидания. Теперь fail-fast.
    - 429 (rate limit) НЕ считается 5xx — для него остаётся обычный путь.
    - Сетевые ошибки (None status) тоже идут обычным путём — возможно,
      следующее зеркало доступно.
    """
    pf = place_filter or PLACE_FILTER
    geom_query = (
        "[out:json][timeout:90];\n"
        "(\n"
        f'  relation["place"~"{pf}"]({bbox_str});\n'
        f'  way["place"~"{pf}"]({bbox_str});\n'
        ");\n"
        "out geom;\n"
    )

    bb_query = (
        "[out:json][timeout:90];\n"
        "(\n"
        f'  relation["place"~"{pf}"]({bbox_str});\n'
        f'  way["place"~"{pf}"]({bbox_str});\n'
        ");\n"
        "out bb;\n"
    )

    # --- Сбор статистики по первому проходу ---
    # Track HTTP-статусы для решения: продолжать ли с bb-fallback или падать.
    first_pass_5xx_count = 0
    first_pass_network_errors = 0
    first_pass_total = 0

    # --- Последовательно пробуем зеркала: geom (1 проход) ---
    # Phase C.4: убран второй проход — при 504 на всех зеркалах он бесполезен.
    for url in OVERPASS_URLS:
        elements, status_code = await _overpass_request(url, geom_query, "geom")
        first_pass_total += 1

        if status_code is not None and 500 <= status_code < 600:
            first_pass_5xx_count += 1
        elif status_code is None:
            first_pass_network_errors += 1

        if elements is not None:
            polygons, is_bbox = _parse_overpass_elements(elements)
            if polygons and not is_bbox:
                logger.info(
                    f"Тайл {tile_idx + 1}/{total_tiles}: "
                    f"{len(polygons)} полигонов (out geom)"
                )
                _save_cache(bbox_str, elements)
                return elements

    # --- Phase C.4: быстрый failover при 5xx на всех зеркалах ---
    # Если все зеркала вернули 5xx (504/502/503) — Overpass лежит глобально,
    # нет смысла делать bb-fallback или второй проход. Падаем с понятной ошибкой.
    if first_pass_5xx_count == first_pass_total and first_pass_total > 0:
        logger.error(
            f"Тайл {tile_idx + 1}/{total_tiles}: все {first_pass_total} зеркал "
            f"Overpass вернули 5xx — сервис недоступен глобально"
        )
        raise RuntimeError(
            "Сервис границ OpenStreetMap (Overpass API) временно недоступен — "
            "HTTP 5xx на всех зеркалах. Попробуйте пересчитать очаги через "
            "несколько минут."
        )

    # --- Fallback: out bb на всех зеркалах ---
    # Делается только если geom НЕ вернул 5xx на всех зеркалах (например,
    # были сетевые ошибки или 429 — тогда bb может помочь).
    for url in OVERPASS_URLS:
        elements, _status = await _overpass_request(url, bb_query, "bb")
        if elements is not None:
            polygons, is_bbox = _parse_overpass_elements(elements)
            if polygons:
                logger.info(
                    f"Тайл {tile_idx + 1}/{total_tiles}: "
                    f"{len(polygons)} bounding boxes (out bb)"
                )
                # НЕ сохраняем bb в кэш
                return elements

    logger.warning(
        f"Тайл {tile_idx + 1}/{total_tiles}: все зеркала недоступны "
        f"(5xx={first_pass_5xx_count}, network={first_pass_network_errors}, "
        f"total={first_pass_total})"
    )
    return None


# ========================
# Классификация ДТП: НП / вне НП
# ========================

def _point_in_any_polygon(
    lat: float,
    lon: float,
    polygons: list[Polygon | MultiPolygon],
) -> bool:
    """
    Попадает ли точка хотя бы в один полигон НП.

    Использует Shapely Point.contains для точной проверки.
    Shapely: (x, y) = (lon, lat).
    """
    point = Point(lon, lat)
    for poly in polygons:
        try:
            if poly.contains(point):
                return True
        except Exception:
            continue
    return False


# Порог числа полигонов, при котором unary_union заменяется на STRtree.
# unary_union(8008 полигонов) создаёт GEOS-геометрию ~300-500 МБ,
# что вызывает OOM Kill на серверах с 2 ГБ RAM.
UNARY_UNION_MAX_POLYGONS = 2000

# Допуск упрощения полигонов OSM (градусы).
# 0.0002° ≈ 22 м — достаточно для определения «ДТП в НП / вне НП».
# Сокращает число вершин в 3-5 раз, экономя ~50-70% памяти на полигонах.
POLYGON_SIMPLIFY_TOLERANCE = 0.0002


def classify_cards(
    cards: list[dict],
    settlement_polygons: list[Polygon | MultiPolygon],
) -> tuple[list[dict], list[dict]]:
    """
    Разделяет карточки на две группы: НП и вне НП.

    При малом числе полигонов (<= UNARY_UNION_MAX_POLYGONS) —
    unary_union + prepared geometry для O(1) на точку.

    При большом числе полигонов — STRtree (пространственный индекс)
    для фильтрации по bounding box перед точной проверкой,
    чтобы избежать OOM от unary_union.

    Args:
        cards: Карточки ДТП с координатами
        settlement_polygons: Список Shapely-полигонов границ НП

    Returns:
        (settlement_cards, non_settlement_cards)
    """
    if not settlement_polygons:
        return [], list(cards)

    settlement_cards = []
    non_settlement_cards = []

    use_strtree = len(settlement_polygons) > UNARY_UNION_MAX_POLYGONS

    if use_strtree:
        # STRtree: пространственный индекс по bounding box полигонов.
        # Позволяет быстро отфильтровать полигоны, чей bbox
        # содержит точку — вместо unary_union всей коллекции.
        logger.info(
            f"Классификация: {len(settlement_polygons)} полигонов — "
            f"используется STRtree (вместо unary_union для экономии памяти)"
        )
        try:
            tree = STRtree(settlement_polygons)
            for card in cards:
                coords = _parse_coords(card)
                if coords is None:
                    non_settlement_cards.append(card)
                    continue
                point = Point(coords[1], coords[0])
                try:
                    candidate_indices = list(tree.query(point))
                    in_settlement = False
                    for idx in candidate_indices:
                        try:
                            if settlement_polygons[idx].contains(point):
                                in_settlement = True
                                break
                        except Exception:
                            continue
                except Exception:
                    in_settlement = False
                if in_settlement:
                    settlement_cards.append(card)
                else:
                    non_settlement_cards.append(card)
            # Освобождаем дерево
            del tree
        except Exception as e:
            logger.warning(
                f"STRtree не удалось: {e}, падаем на поцикличную проверку"
            )
            # Fallback: поцикличная проверка
            for card in cards:
                coords = _parse_coords(card)
                if coords is None:
                    non_settlement_cards.append(card)
                    continue
                if _point_in_any_polygon(coords[0], coords[1], settlement_polygons):
                    settlement_cards.append(card)
                else:
                    non_settlement_cards.append(card)
    else:
        # Мало полигонов — unary_union + prepared geometry (быстро и мало памяти)
        try:
            merged = unary_union(settlement_polygons)
            prepared = prep(merged)
            use_prepared = True
        except Exception as e:
            logger.warning(
                f"Не удалось создать prepared geometry: {e}. "
                f"Используется поцикличная проверка."
            )
            prepared = None
            use_prepared = False

        for card in cards:
            coords = _parse_coords(card)
            if coords is None:
                non_settlement_cards.append(card)
                continue

            point = Point(coords[1], coords[0])
            in_settlement = False

            try:
                if use_prepared and prepared is not None:
                    in_settlement = prepared.contains(point)
                else:
                    in_settlement = _point_in_any_polygon(
                        coords[0], coords[1], settlement_polygons,
                    )
            except Exception:
                pass

            if in_settlement:
                settlement_cards.append(card)
            else:
                non_settlement_cards.append(card)

        # Освобождаем merged/prepared
        del merged
        del prepared

    logger.info(
        f"Классификация: {len(settlement_cards)} в НП, "
        f"{len(non_settlement_cards)} вне НП "
        f"(всего {len(cards)}, полигонов: {len(settlement_polygons)})"
    )
    return settlement_cards, non_settlement_cards


# ========================
# Причины ДТП (счётчики для Excel и LLM)
# ========================

_SOP_NPDD_FILTER_WORDS = ("опьянение", "лишенным", "имеющим")

# Значения, которые означают «нет данных» — не включаем в счётчики
_CAUSE_SKIP_VALUES = frozenset({
    "нет нарушений",
    "не установлены",
    "сведения отсутствуют",
    "технические неисправности отсутствуют",
})


