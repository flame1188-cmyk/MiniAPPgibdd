"""
API для работы с полигонами границ населённых пунктов.

Endpoints:
  GET  /api/polygons/check-access   — проверка доступа к редактору
  GET  /api/polygons/regions         — список регионов с полигонами в БД
  GET  /api/polygons/{region_code}  — GeoJSON FeatureCollection
  PUT  /api/polygons/{polygon_id}   — сохранить отредактированный полигон
  POST /api/polygons/{region_code}/import — импорт из JSON-кэша в БД
  POST /api/polygons/{region_code}/reset/{pid} — сброс к оригиналу
  GET  /api/polygons/editors        — список редакторов
  POST /api/polygons/editors        — добавить редактора
  DELETE /api/polygons/editors/{tid} — удалить редактора
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from typing import Optional

from ..telegram_auth import get_current_user, TelegramUser
from ..db.connection import get_pool
from ..db.polygon_repository import (
    get_regions_with_polygons,
    load_geojson_from_db,
    update_polygon_geometry,
    reset_polygon_to_original,
    save_polygons_to_db,
    create_polygon,
    is_polygon_editor,
    get_polygon_editors,
    add_polygon_editor,
    remove_polygon_editor,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/polygons", tags=["polygons"])


# ========================
# Pydantic модели
# ========================

class UpdateGeometryRequest(BaseModel):
    geometry: dict  # GeoJSON Geometry


class AddEditorRequest(BaseModel):
    telegram_id: int
    name: str = ""


class CreatePolygonRequest(BaseModel):
    geometry: dict  # GeoJSON Geometry
    name: str = ""
    place_type: str = ""


class ImportRequest(BaseModel):
    force: bool = False  # Перезаписывать даже is_edited=true


# ========================
# Хелпер: проверка доступа
# ========================

async def _require_editor(user: TelegramUser) -> None:
    """Проверяет, что пользователь есть в списке редакторов."""
    pool = get_pool()
    if pool is None:
        raise HTTPException(503, "БД недоступна")
    if not await is_polygon_editor(pool, user.id):
        raise HTTPException(403, "Нет доступа к редактору полигонов")


# ========================
# Публичные эндпоинты (для всех авторизованных)
# ========================

@router.get("/check-access")
async def check_access(user: TelegramUser = Depends(get_current_user)):
    """Проверяет, имеет ли текущий пользователь доступ к редактору."""
    pool = get_pool()
    if pool is None:
        return {"is_editor": False}
    return {"is_editor": await is_polygon_editor(pool, user.id)}


@router.get("/regions")
async def list_regions():
    """Список регионов, для которых есть полигоны в БД."""
    pool = get_pool()
    if pool is None:
        return []
    return await get_regions_with_polygons(pool)


@router.get("/{region_code}")
async def get_region_polygons(
    region_code: str,
    simplified: bool = Query(False, description="Упрощённая геометрия для обзора"),
):
    """Возвращает полигоны региона как GeoJSON FeatureCollection."""
    pool = get_pool()
    if pool is None:
        raise HTTPException(503, "БД недоступна")

    data = await load_geojson_from_db(pool, region_code, simplified=simplified)
    if data is None:
        raise HTTPException(404, f"Полигоны региона {region_code} не найдены в БД")
    return data


# ========================
# Эндпоинты для редакторов
# ========================

@router.put("/{polygon_id}")
async def update_polygon(
    polygon_id: int,
    req: UpdateGeometryRequest,
    user: TelegramUser = Depends(get_current_user),
):
    """Сохраняет отредактированную геометрию полигона."""
    await _require_editor(user)
    pool = get_pool()
    if pool is None:
        raise HTTPException(503, "БД недоступна")

    ok = await update_polygon_geometry(pool, polygon_id, req.geometry, user.id)
    if not ok:
        raise HTTPException(404, f"Полигон {polygon_id} не найден")
    return {"status": "ok", "id": polygon_id}


@router.post("/{region_code}/import")
async def import_region_from_cache(
    region_code: str,
    req: ImportRequest = ImportRequest(),
    user: TelegramUser = Depends(get_current_user),
):
    """
    Импортирует полигоны региона из JSON-кэша в БД.

    Использует существующий region_{code}.json.
    При force=True перезаписывает даже вручную отредактированные.
    """
    await _require_editor(user)
    pool = get_pool()
    if pool is None:
        raise HTTPException(503, "БД недоступна")

    from concentration_points._osm_boundaries import _load_region_cache

    elements = _load_region_cache(region_code)
    if elements is None:
        raise HTTPException(
            404,
            f"JSON-кэш региона {region_code} не найден. "
            f"Сначала выполните precache_osm.py --codes {region_code}",
        )

    # При force сбрасываем is_edited, чтобы upsert перезаписал всё
    if req.force:
        async with pool.connection() as conn:
            await conn.execute(
                "DELETE FROM settlement_polygons WHERE region_code = %s",
                (region_code,),
            )
            await conn.commit()

    count = await save_polygons_to_db(pool, region_code, elements)
    return {
        "status": "ok",
        "region_code": region_code,
        "imported": count,
        "source_elements": len(elements),
    }


@router.post("/{region_code}/reset/{polygon_id}")
async def reset_polygon(
    region_code: str,
    polygon_id: int,
    user: TelegramUser = Depends(get_current_user),
):
    """Сбрасывает полигон к оригинальной версии из JSON-кэша."""
    await _require_editor(user)
    pool = get_pool()
    if pool is None:
        raise HTTPException(503, "БД недоступна")

    # Получаем osm_type и osm_id для поиска в JSON-кэше
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT osm_type, osm_id FROM settlement_polygons WHERE id = %s",
            (polygon_id,),
        )
        row = await cur.fetchone()

    if not row:
        raise HTTPException(404, f"Полигон {polygon_id} не найден")

    osm_type = row["osm_type"] if isinstance(row, dict) else row[0]
    osm_id = row["osm_id"] if isinstance(row, dict) else row[1]

    ok = await reset_polygon_to_original(
        pool, polygon_id, region_code, osm_type, osm_id
    )
    if not ok:
        raise HTTPException(404, "Не удалось сбросить (элемент не найден в JSON-кэше)")
    return {"status": "ok", "id": polygon_id, "reset": True}


# ========================
# Создание нового полигона (для редакторов)
# ========================

@router.post("/{region_code}/create")
async def create_new_polygon(
    region_code: str,
    req: CreatePolygonRequest,
    user: TelegramUser = Depends(get_current_user),
):
    """Создаёт новый пользовательский полигон в регионе."""
    await _require_editor(user)
    pool = get_pool()
    if pool is None:
        raise HTTPException(503, "БД недоступна")

    new_id = await create_polygon(
        pool, region_code, req.geometry, req.name, req.place_type, user.id
    )
    if new_id is None:
        raise HTTPException(500, "Не удалось создать полигон")
    return {"status": "ok", "id": new_id}


# ========================
# Управление редакторами (только для редакторов)
# ========================

@router.get("/editors")
async def list_editors(user: TelegramUser = Depends(get_current_user)):
    """Возвращает список пользователей с доступом к редактору."""
    await _require_editor(user)
    pool = get_pool()
    if pool is None:
        raise HTTPException(503, "БД недоступна")
    return await get_polygon_editors(pool)


@router.post("/editors")
async def add_editor(req: AddEditorRequest, user: TelegramUser = Depends(get_current_user)):
    """Добавляет пользователя в список редакторов."""
    await _require_editor(user)
    pool = get_pool()
    if pool is None:
        raise HTTPException(503, "БД недоступна")

    is_new = await add_polygon_editor(pool, req.telegram_id, req.name)
    return {
        "status": "ok",
        "telegram_id": req.telegram_id,
        "is_new": is_new,
    }


@router.delete("/editors/{telegram_id}")
async def delete_editor(telegram_id: int, user: TelegramUser = Depends(get_current_user)):
    """Удаляет пользователя из списка редакторов."""
    await _require_editor(user)
    pool = get_pool()
    if pool is None:
        raise HTTPException(503, "БД недоступна")

    ok = await remove_polygon_editor(pool, telegram_id)
    if not ok:
        raise HTTPException(404, f"Редактор {telegram_id} не найден")
    return {"status": "ok", "removed": True}
