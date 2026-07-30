"""
Роутер для статистики ДТП по географической точке.

POST /api/point — получить сводку ДТП в радиусе от (lat, lon).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..services.gibdd_service import get_point_statistics
from ..telegram_auth import TelegramUser, get_current_user

router = APIRouter(prefix="/point", tags=["point"])


class PointRequest(BaseModel):
    lat: float = Field(..., ge=-90, le=90, description="Широта")
    lon: float = Field(..., ge=-180, le=180, description="Долгота")
    radius_km: float = Field(default=2.0, gt=0, le=50,
                              description="Радиус в километрах")


@router.post("")
async def point_statistics(
    request: PointRequest,
    user: TelegramUser = Depends(get_current_user),
):
    """Возвращает статистику ДТП в радиусе от заданной точки."""
    return await get_point_statistics(
        lat=request.lat,
        lon=request.lon,
        radius_km=request.radius_km,
    )
