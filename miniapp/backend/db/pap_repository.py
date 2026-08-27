"""
Запросы к gibdd_db для получения данных ПАП.

Агрегирует ПАП по координатам (lat/lon) за выбранный период,
объединяя статьи в JSON-массив.
"""
from __future__ import annotations

import calendar
import json
import logging
from typing import Any

from .pap_connection import get_pap_pool, is_pap_db_ready
from .pap_region_mapping import get_pap_region_id

logger = logging.getLogger(__name__)


def _dat_list_to_date_range(dat_list: list[str]) -> tuple[str, str]:
    """
    Преобразует список ['2025-01', '2025-02', ...] в (min_date, max_date_exclusive).
    
    Возвращает:
        ('2025-01-01', '2025-07-01') — для SQL WHERE date >= ... AND date < ...
    """
    if not dat_list:
        return "", ""
    
    # Сортируем и берём крайние месяцы
    months = sorted(dat_list)
    first = months[0]  # '2025-01'
    last = months[-1]   # '2025-06'
    
    # min_date = первый день первого месяца
    min_date = f"{first}-01"
    
    # max_date = первый день месяца СЛЕДУЮЩЕГО за последним
    year = int(last.split("-")[0])
    month = int(last.split("-")[1])
    month += 1
    if month > 12:
        month = 1
        year += 1
    max_date = f"{year:04d}-{month:02d}-01"
    
    return min_date, max_date


async def fetch_pap_for_map(
    app_region_code: str,
    dat_list: list[str],
) -> list[dict[str, Any]]:
    """
    Загружает ПАП для карты, агрегированные по координатам.
    
    Возвращает список dicts:
        [{
            "lat": 56.847,
            "lon": 60.608,
            "total": 184,
            "repeat": 1,
            "articles": [
                {"article": "Статья 12.6", "group": "Ремни", "cnt": 150, "repeat": 1},
                ...
            ]
        }, ...]
    
    Если регион не найден в маппинге или ПАП БД недоступна — []
    """
    if not is_pap_db_ready():
        return []
    
    gibdd_region_id = get_pap_region_id(app_region_code)
    if gibdd_region_id is None:
        logger.debug(f"PAP: нет маппинга для региона {app_region_code}")
        return []
    
    min_date, max_date = _dat_list_to_date_range(dat_list)
    if not min_date or not max_date:
        return []
    
    pool = get_pap_pool()
    if pool is None:
        return []
    
    sql = """
    SELECT 
        p.lat,
        p.lon,
        SUM(p.pap_cnt)::int AS total_pap,
        SUM(p.repeat_cnt)::int AS total_repeat,
        COALESCE(
            json_agg(
                json_build_object(
                    'article', a.article_num,
                    'group', a.viol_group,
                    'cnt', p.pap_cnt,
                    'repeat', p.repeat_cnt
                )
                ORDER BY p.pap_cnt DESC
            ) FILTER (WHERE p.koap_id IS NOT NULL),
            '[]'::json
        ) AS articles
    FROM gibdd.paps p
    LEFT JOIN gibdd.articles a ON a.koap_id = p.koap_id
    WHERE p.region_id = %(region_id)s
      AND p.date >= %(min_date)s
      AND p.date < %(max_date)s
      AND p.lat IS NOT NULL AND p.lon IS NOT NULL
    GROUP BY p.lat, p.lon
    ORDER BY total_pap DESC
    """
    
    try:
        async with pool.connection() as conn:
            cur = await conn.execute(
                sql,
                {"region_id": gibdd_region_id, "min_date": min_date, "max_date": max_date},
            )
            rows = await cur.fetchall()
    
    except Exception as exc:
        logger.warning(f"PAP: запрос к gibdd_db failed: {exc}")
        return []
    
    result = []
    for row in rows:
        articles_raw = row["articles"]
        if isinstance(articles_raw, str):
            articles_raw = json.loads(articles_raw)
        result.append({
            "lat": float(row["lat"]),
            "lon": float(row["lon"]),
            "total": row["total_pap"],
            "repeat": row["total_repeat"],
            "articles": articles_raw or [],
        })
    
    logger.info(
        f"PAP: регион {app_region_code} -> gibdd_id={gibdd_region_id}, "
        f"период {min_date}..{max_date}, "
        f"загружено {len(result)} точек"
    )
    return result
