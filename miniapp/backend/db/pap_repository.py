"""
Запросы ПАП из локальной таблицы pap_points (основная БД).

Данные попадают в pap_points через скрипт scripts/sync_pap.py,
который запускается вручную с VPN-доступом к gibdd_db.

Агрегирует ПАП по координатам (lat/lon) за выбранный период,
объединяя статьи в JSON-массив — тот же формат, что раньше
возвращал прямой запрос к gibdd_db.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from .connection import get_pool

logger = logging.getLogger(__name__)


def _dat_list_to_date_range(dat_list: list[str]) -> tuple[str, str]:
    """
    Преобразует список месяцев в (min_date, max_date_exclusive).

    Поддерживает два формата:
      ['1.2026', '2.2026', ...]  — формат приложения (M.YYYY)
      ['2026-01', '2026-02', ...] — ISO-формат (YYYY-MM)

    Возвращает:
        ('2026-01-01', '2026-08-01') — для SQL WHERE date >= ... AND date < ...
    """
    if not dat_list:
        return "", ""

    def _parse(s: str) -> tuple[int, int]:
        """Парсит 'M.YYYY' или 'YYYY-MM' в (year, month)."""
        if "." in s:
            m, y = s.split(".")
            return int(y), int(m)
        else:
            y, m = s.split("-")
            return int(y), int(m)

    months = sorted(dat_list, key=_parse)
    first_y, first_m = _parse(months[0])
    last_y, last_m = _parse(months[-1])

    min_date = f"{first_y:04d}-{first_m:02d}-01"

    last_m += 1
    if last_m > 12:
        last_m = 1
        last_y += 1
    max_date = f"{last_y:04d}-{last_m:02d}-01"

    return min_date, max_date


async def fetch_pap_for_map(
    app_region_code: str,
    dat_list: list[str],
) -> list[dict[str, Any]]:
    """
    Загружает ПАП для карты из локальной таблицы pap_points.

    Агрегирует по координатам (lat/lon), объединяя статьи в JSON.

    Возвращает список dicts:
        [{
            "lat": 56.847,
            "lon": 60.608,
            "total": 184,
            "articles": [
                {"article": "12.6", "group": "Ремни", "cnt": 150},
                ...
            ]
        }, ...]

    Если БД недоступна или данных нет — [].
    """
    pool = get_pool()
    if pool is None:
        return []

    min_date, max_date = _dat_list_to_date_range(dat_list)
    if not min_date or not max_date:
        return []

    sql = """
    -- Шаг 1: агрегируем по (lat, lon, article_num, viol_group)
    WITH per_article AS (
        SELECT
            lat,
            lon,
            article_num,
            viol_group,
            SUM(pap_cnt)::int    AS cnt
        FROM pap_points
        WHERE app_region_code = %(region_code)s
          AND date >= %(min_date)s
          AND date < %(max_date)s
          AND koap_id IS NOT NULL AND koap_id != -1
        GROUP BY lat, lon, article_num, viol_group
    ),
    -- Шаг 2: агрегируем по (lat, lon)
    point_agg AS (
        SELECT
            lat,
            lon,
            SUM(cnt)::int    AS total_pap
        FROM per_article
        GROUP BY lat, lon
    ),
    -- Шаг 3: статьи как JSON для каждой точки
    point_articles AS (
        SELECT
            pa.lat,
            pa.lon,
            COALESCE(
                json_agg(
                    json_build_object(
                        'article', per.article_num,
                        'group', per.viol_group,
                        'cnt', per.cnt
                    )
                    ORDER BY per.cnt DESC
                ),
                '[]'::json
            ) AS articles
        FROM point_agg pa
        LEFT JOIN per_article per
            ON per.lat = pa.lat AND per.lon = pa.lon
        GROUP BY pa.lat, pa.lon
    )
    SELECT
        pa.lat,
        pa.lon,
        pa.total_pap,
        COALESCE(part.articles, '[]'::json) AS articles
    FROM point_agg pa
    LEFT JOIN point_articles part
        ON part.lat = pa.lat AND part.lon = pa.lon
    ORDER BY pa.total_pap DESC
    """

    try:
        async with pool.connection() as conn:
            cur = await conn.execute(
                sql,
                {
                    "region_code": app_region_code,
                    "min_date": min_date,
                    "max_date": max_date,
                },
            )
            rows = await cur.fetchall()

    except Exception as exc:
        logger.warning(f"PAP: запрос к pap_points failed: {exc}")
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
            "articles": articles_raw or [],
        })

    logger.info(
        f"PAP: регион {app_region_code}, "
        f"период {min_date}..{max_date}, "
        f"загружено {len(result)} точек"
    )
    return result
