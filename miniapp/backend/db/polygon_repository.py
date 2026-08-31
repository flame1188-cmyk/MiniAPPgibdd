"""
Репозиторий полигонов границ населённых пунктов (settlement_polygons).

Операции:
  - save_polygons_to_db: массовая загрузка из Overpass elements
  - load_polygons_from_db: загрузка Shapely-полигонов для classify_cards
  - load_geojson_from_db: загрузка GeoJSON для фронтенда/редактора
  - update_polygon_geometry: сохранение отредактированного полигона
  - get_regions_with_polygons: список регионов с загруженными полигонами
  - is_polygon_editor: проверка доступа к редактору
"""
from __future__ import annotations

import json
import logging
from typing import Optional

import shapely
from shapely.geometry import Polygon, MultiPolygon

from .connection import get_pool

logger = logging.getLogger(__name__)


# ========================
# Конвертация Overpass → GeoJSON
# ========================

def _overpass_element_to_geojson(element: dict) -> Optional[dict]:
    """
    Конвертирует один Overpass-элемент (out geom) в GeoJSON Feature.

    Использует существующие функции _way_to_polygon/_relation_to_polygon
    из _osm_boundaries для разбора геометрии, затем shapely.to_geojson().

    Returns:
        GeoJSON Feature dict или None, если полигон не удалось разобрать.
    """
    from concentration_points._osm_boundaries import (
        _way_to_polygon, _relation_to_polygon,
    )

    el_type = element.get("type", "")
    el_id = element.get("id", 0)
    tags = element.get("tags", {})
    name = tags.get("name", "")
    place_type = tags.get("place", "")

    poly = None
    if el_type == "way":
        poly = _way_to_polygon(element)
    elif el_type == "relation":
        poly = _relation_to_polygon(element)

    if poly is None:
        return None

    # Shapely → GeoJSON (координаты в порядке [lon, lat])
    try:
        geojson_str = shapely.to_geojson(poly)
        geometry = json.loads(geojson_str)
    except Exception as e:
        logger.warning(f"Не удалось конвертировать полигон {el_type}/{el_id}: {e}")
        return None

    return {
        "type": "Feature",
        "properties": {
            "name": name,
            "place_type": place_type,
            "osm_type": el_type,
            "osm_id": el_id,
        },
        "geometry": geometry,
    }


def _elements_to_geojson_features(
    elements: list[dict],
) -> list[dict]:
    """
    Конвертирует список Overpass-элементов в список GeoJSON Feature.

    Пропускает элементы, не разобравшиеся в полигоны.
    """
    features = []
    for el in elements:
        feature = _overpass_element_to_geojson(el)
        if feature is not None:
            features.append(feature)
    return features


# ========================
# Сохранение в БД
# ========================

async def save_polygons_to_db(
    pool,
    reg_code: str,
    elements: list[dict],
) -> int:
    """
    Массовая загрузка полигонов региона в БД.

    Конвертирует Overpass-элементы в GeoJSON и записывает через
    INSERT ... ON CONFLICT DO UPDATE (upsert).

    Args:
        pool: AsyncConnectionPool
        reg_code: код региона ГИБДД ("1146")
        elements: сырые элементы из Overpass (out geom)

    Returns:
        Количество сохранённых полигонов.
    """
    features = _elements_to_geojson_features(elements)
    if not features:
        logger.warning(f"Нет полигонов для сохранения в БД (регион {reg_code})")
        return 0

    rows = []
    for f in features:
        props = f["properties"]
        rows.append((
            reg_code,
            props["osm_type"],
            props["osm_id"],
            props["name"],
            props["place_type"],
            json.dumps(f["geometry"], ensure_ascii=False),
        ))

    async with pool.connection() as conn:
        # Пакетная вставка чанками по 500 (без psycopg.extras.execute_values,
        # который недоступен в некоторых версиях psycopg 3.x).
        CHUNK = 500
        for i in range(0, len(rows), CHUNK):
            chunk = rows[i:i + CHUNK]
            placeholders = ",".join(
                "(%s, %s, %s, %s, %s, %s::jsonb, false, NULL, NULL)"
                for _ in chunk
            )
            query = f"""
                INSERT INTO settlement_polygons
                    (region_code, osm_type, osm_id, name, place_type, geometry,
                     is_edited, edited_at, edited_by)
                VALUES {placeholders}
                ON CONFLICT (region_code, osm_type, osm_id)
                DO UPDATE SET
                    name = EXCLUDED.name,
                    place_type = EXCLUDED.place_type,
                    geometry = EXCLUDED.geometry
                WHERE settlement_polygons.is_edited = false
            """
            flat_params = [p for row in chunk for p in row]
            await conn.execute(query, flat_params)
        await conn.commit()

    logger.info(
        f"Полигоны региона {reg_code}: {len(rows)} записей в БД "
        f"(из {len(elements)} элементов Overpass)"
    )
    return len(rows)


# ========================
# Загрузка из БД
# ========================

async def load_polygons_from_db(
    pool,
    reg_code: str,
) -> Optional[list[Polygon | MultiPolygon]]:
    """
    Загружает полигоны региона из БД как Shapely-объекты.

    Используется в _osm_boundaries.py для classify_cards.
    GeoJSON → shapely.from_geojson().

    Returns:
        Список Shapely полигонов или None, если в БД нет данных.
    """
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT geometry FROM settlement_polygons WHERE region_code = %s",
            (reg_code,),
        )
        rows = await cur.fetchall()

    if not rows:
        return None

    polygons = []
    for row in rows:
        geom_json = row["geometry"] if isinstance(row, dict) else row[0]
        try:
            # geom_json может быть str или dict (psycopg JSONB → dict)
            if isinstance(geom_json, str):
                geom_json = json.loads(geom_json)
            poly = shapely.from_geojson(json.dumps(geom_json))
            if poly is not None and not poly.is_empty:
                polygons.append(poly)
        except Exception as e:
            logger.warning(f"Ошибка парсинга полигона из БД: {e}")
            continue

    if polygons:
        logger.info(
            f"Полигоны региона {reg_code} из БД: {len(polygons)} полигонов"
        )
    return polygons if polygons else None


async def load_geojson_from_db(
    pool,
    reg_code: str,
    simplified: bool = False,
) -> Optional[dict]:
    """
    Загружает полигоны региона как GeoJSON FeatureCollection.

    Для фронтенда/редактора карт.
    При simplified=True упрощает геометрию (tolerance ~0.001°) для
    отображения на карте при низком зуме.

    Returns:
        GeoJSON FeatureCollection dict или None.
    """
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT id, osm_type, osm_id, name, place_type,
                   geometry, is_edited, edited_at, edited_by
            FROM settlement_polygons
            WHERE region_code = %s
            ORDER BY name
            """,
            (reg_code,),
        )
        rows = await cur.fetchall()

    if not rows:
        return None

    features = []
    for row in rows:
        geom_json = row["geometry"] if isinstance(row, dict) else row[5]

        # Упрощение для отображения на карте
        if simplified and geom_json:
            try:
                if isinstance(geom_json, str):
                    geom_json = json.loads(geom_json)
                poly = shapely.from_geojson(json.dumps(geom_json))
                if poly is not None:
                    s = poly.simplify(0.001, preserve_topology=True)
                    if not s.is_empty and s.area > 1e-10:
                        geom_json = json.loads(shapely.to_geojson(s))
            except Exception:
                pass  # Фолбэк на полную геометрию

        feature = {
            "type": "Feature",
            "properties": {
                "id": row["id"] if isinstance(row, dict) else row[0],
                "osm_type": row["osm_type"] if isinstance(row, dict) else row[1],
                "osm_id": row["osm_id"] if isinstance(row, dict) else row[2],
                "name": row["name"] if isinstance(row, dict) else row[3],
                "place_type": row["place_type"] if isinstance(row, dict) else row[4],
                "is_edited": row["is_edited"] if isinstance(row, dict) else row[6],
                "edited_at": str(row["edited_at"]) if (isinstance(row, dict) and row.get("edited_at")) else None,
                "edited_by": row["edited_by"] if isinstance(row, dict) else row[7],
            },
            "geometry": geom_json,
        }
        features.append(feature)

    return {
        "type": "FeatureCollection",
        "features": features,
    }


# ========================
# Редактирование
# ========================

async def update_polygon_geometry(
    pool,
    polygon_id: int,
    geometry_geojson: dict,
    edited_by: int,
) -> bool:
    """
    Обновляет геометрию полигона (после редактирования на карте).

    Args:
        polygon_id: ID записи в settlement_polygons
        geometry_geojson: новая геометрия в формате GeoJSON
        edited_by: Telegram user ID

    Returns:
        True если обновлено, False если не найдено.
    """
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            UPDATE settlement_polygons
            SET geometry = %s::jsonb,
                is_edited = true,
                edited_at = now(),
                edited_by = %s
            WHERE id = %s
            """,
            (json.dumps(geometry_geojson, ensure_ascii=False), edited_by, polygon_id),
        )
        await conn.commit()
        return cur.rowcount > 0


async def reset_polygon_to_original(
    pool,
    polygon_id: int,
    reg_code: str,
    osm_type: str,
    osm_id: int,
) -> bool:
    """
    Сбрасывает полигон к версии из JSON-кэша (отменяет ручную правку).

    Перечитывает элемент из region_*.json и перезаписывает geometry в БД.
    """
    from concentration_points._osm_boundaries import (
        _load_region_cache, _way_to_polygon, _relation_to_polygon,
    )

    elements = _load_region_cache(reg_code)
    if elements is None:
        return False

    # Ищем нужный элемент
    target_element = None
    for el in elements:
        if el.get("type") == osm_type and el.get("id") == osm_id:
            target_element = el
            break

    if target_element is None:
        return False

    # Конвертируем обратно в GeoJSON
    feature = _overpass_element_to_geojson(target_element)
    if feature is None:
        return False

    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            UPDATE settlement_polygons
            SET geometry = %s::jsonb,
                is_edited = false,
                edited_at = NULL,
                edited_by = NULL
            WHERE id = %s
            """,
            (json.dumps(feature["geometry"], ensure_ascii=False), polygon_id),
        )
        await conn.commit()
        return cur.rowcount > 0


# ========================
# Справочники
# ========================

async def get_regions_with_polygons(pool) -> list[dict]:
    """
    Возвращает список регионов, для которых есть полигоны в БД.

    Returns:
        [{"region_code": "1146", "count": 9392}, ...]
    """
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT region_code, count(*) as polygon_count
            FROM settlement_polygons
            GROUP BY region_code
            ORDER BY region_code
            """
        )
        rows = await cur.fetchall()

    return [
        {
            "region_code": r["region_code"] if isinstance(r, dict) else r[0],
            "count": r["polygon_count"] if isinstance(r, dict) else r[1],
        }
        for r in rows
    ]


async def is_polygon_editor(pool, telegram_id: int) -> bool:
    """Проверяет, есть ли у пользователя доступ к редактору полигонов."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT 1 FROM polygon_editors WHERE telegram_id = %s",
            (telegram_id,),
        )
        row = await cur.fetchone()
        return row is not None


async def get_polygon_editors(pool) -> list[dict]:
    """Возвращает список редакторов полигонов."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT telegram_id, name, added_at FROM polygon_editors ORDER BY added_at"
        )
        rows = await cur.fetchall()

    return [
        {
            "telegram_id": r["telegram_id"] if isinstance(r, dict) else r[0],
            "name": r["name"] if isinstance(r, dict) else r[1],
            "added_at": str(r["added_at"]) if isinstance(r, dict) else str(r[2]),
        }
        for r in rows
    ]


async def add_polygon_editor(pool, telegram_id: int, name: str = "") -> bool:
    """Добавляет пользователя в список редакторов. Возвращает True если новый."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            INSERT INTO polygon_editors (telegram_id, name)
            VALUES (%s, %s)
            ON CONFLICT (telegram_id) DO NOTHING
            """,
            (telegram_id, name),
        )
        await conn.commit()
        return cur.rowcount > 0


async def remove_polygon_editor(pool, telegram_id: int) -> bool:
    """Удаляет пользователя из списка редакторов."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "DELETE FROM polygon_editors WHERE telegram_id = %s",
            (telegram_id,),
        )
        await conn.commit()
        return cur.rowcount > 0
