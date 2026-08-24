"""metrics.py — SQL-агрегация метрик ДТП из архива PostgreSQL.

Вычисляет тот же словарь, что analytics.calculate_metrics(),
но напрямую SQL-запросами к gibdd_cards/gibdd_participants
без загрузки сырых карточек в память Python.

Используется для:
  - Сравнения с АППГ и мультигодовой динамики (быстро, без RAM)
  - Текущий период по-прежнему через calculate_metrics() (нужен для LLM/очагов)

Все запросы используют индексы:
  - idx_cards_reg_dat (reg_code, dat_period)
  - idx_participants_card (card_id)
  - idx_participants_alco (alco) WHERE alco != '00'
  - idx_participants_kt (kt_uch)
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from .connection import get_pool, is_db_ready

logger = logging.getLogger(__name__)

# Русские названия месяцев (совпадают с analytics.MONTH_FULL)
_MONTH_NAMES = {
    1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель",
    5: "Май", 6: "Июнь", 7: "Июль", 8: "Август",
    9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь",
}


async def calculate_metrics_from_db(
    reg_code: str,
    dat_list: list[str],
) -> dict[str, Any] | None:
    """Агрегирует метрики ДТП из PostgreSQL за (reg_code, dat_list).

    Возвращает dict, совместимый с analytics.calculate_metrics(),
    или None если БД недоступна / нет данных.
    """
    if not is_db_ready() or not dat_list:
        return None

    pool = get_pool()
    if pool is None:
        return None

    try:
        return await _compute_all(pool, reg_code, dat_list)
    except Exception as e:
        logger.warning(f"SQL metrics failed: {e}, falling back to Python")
        return None


async def _compute_all(
    pool,
    reg_code: str,
    dat_list: list[str],
) -> dict[str, Any]:
    """Запускает все SQL-запросы параллельно и собирает результат."""

    async def _q1_totals() -> tuple[int, int, int]:
        """Всего ДТП, погибших, раненых."""
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    SELECT COUNT(*) AS total,
                           COALESCE(SUM(pog), 0) AS deaths,
                           COALESCE(SUM(ran), 0) AS injured
                    FROM gibdd_cards
                    WHERE reg_code = %(reg)s AND dat_period = ANY(%(dats)s)
                """, {"reg": reg_code, "dats": dat_list})
                r = await cur.fetchone()
                return (r["total"], int(r["deaths"]), int(r["injured"]))

    async def _q2_type() -> list[dict]:
        """Группировка по виду ДТП (dtpv) с тяжестью."""
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    SELECT dtpv,
                           COUNT(*) AS dtp,
                           COALESCE(SUM(pog), 0) AS deaths,
                           COALESCE(SUM(ran), 0) AS injured
                    FROM gibdd_cards
                    WHERE reg_code = %(reg)s AND dat_period = ANY(%(dats)s)
                      AND dtpv IS NOT NULL AND dtpv != ''
                    GROUP BY dtpv
                """, {"reg": reg_code, "dats": dat_list})
                return await cur.fetchall()

    async def _q3_hour() -> list[dict]:
        """Группировка по часу с тяжестью."""
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    SELECT EXTRACT(HOUR FROM time)::int AS hour,
                           COUNT(*) AS dtp,
                           COALESCE(SUM(pog), 0) AS deaths,
                           COALESCE(SUM(ran), 0) AS injured
                    FROM gibdd_cards
                    WHERE reg_code = %(reg)s AND dat_period = ANY(%(dats)s)
                      AND time IS NOT NULL
                    GROUP BY EXTRACT(HOUR FROM time)
                """, {"reg": reg_code, "dats": dat_list})
                return await cur.fetchall()

    async def _q4_weekday() -> list[dict]:
        """Группировка по дню недели (ПН=0) с тяжестью.
        
        PostgreSQL ISODOW: ПН=1, ВТ=2, ... ВС=7.
        Python: ПН=0, ВТ=1, ... ВС=6.
        """
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    SELECT (EXTRACT(ISODOW FROM date_dtp)::int - 1) AS wd,
                           COUNT(*) AS dtp,
                           COALESCE(SUM(pog), 0) AS deaths,
                           COALESCE(SUM(ran), 0) AS injured
                    FROM gibdd_cards
                    WHERE reg_code = %(reg)s AND dat_period = ANY(%(dats)s)
                    GROUP BY EXTRACT(ISODOW FROM date_dtp)
                """, {"reg": reg_code, "dats": dat_list})
                return await cur.fetchall()

    async def _q5_month() -> list[dict]:
        """Группировка по месяцу с тяжестью."""
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    SELECT EXTRACT(MONTH FROM date_dtp)::int AS month,
                           COUNT(*) AS dtp,
                           COALESCE(SUM(pog), 0) AS deaths,
                           COALESCE(SUM(ran), 0) AS injured
                    FROM gibdd_cards
                    WHERE reg_code = %(reg)s AND dat_period = ANY(%(dats)s)
                    GROUP BY EXTRACT(MONTH FROM date_dtp)
                """, {"reg": reg_code, "dats": dat_list})
                return await cur.fetchall()

    async def _q6_road() -> list[dict]:
        """Группировка по dor (дорога) и dor_z (значение) с тяжестью."""
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    SELECT dor,
                           COALESCE(dor_z, '') AS dor_z,
                           COUNT(*) AS dtp,
                           COALESCE(SUM(pog), 0) AS deaths,
                           COALESCE(SUM(ran), 0) AS injured
                    FROM gibdd_cards
                    WHERE reg_code = %(reg)s AND dat_period = ANY(%(dats)s)
                    GROUP BY dor, dor_z
                """, {"reg": reg_code, "dats": dat_list})
                return await cur.fetchall()

    async def _q7_alcohol() -> int:
        """Количество ДТП с нетрезвыми водителями."""
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                # DIAG: считаем диагностику отдельно от основного запроса
                await cur.execute("""
                    SELECT COUNT(DISTINCT p.card_id) AS cnt,
                           COUNT(*) AS total_drivers,
                           COUNT(p.alco) AS alco_not_null,
                           COUNT(CASE WHEN p.alco NOT IN ('0', '00', '') THEN 1 END) AS alco_non_zero
                    FROM gibdd_participants p
                    JOIN gibdd_cards c ON c.id = p.card_id
                    WHERE c.reg_code = %(reg)s
                      AND c.dat_period = ANY(%(dats)s)
                      AND p.kt_uch ILIKE '%%водитель%%'
                      AND p.alco IS NOT NULL
                      AND p.alco NOT IN ('0', '00', '')
                """, {"reg": reg_code, "dats": dat_list})
                diag = await cur.fetchone()
                logger.info(
                    f"[DIAG q7_alcohol] reg={reg_code} dats={dat_list}: "
                    f"cnt={diag['cnt']}, total_drivers={diag['total_drivers']}, "
                    f"alco_not_null={diag['alco_not_null']}, alco_non_zero={diag['alco_non_zero']}"
                )
                return int(diag["cnt"]) if diag else 0

    async def _q8_pedestrian() -> int:
        """Количество ДТП с пешеходами (по участникам ИЛИ по dtpv)."""
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    SELECT COUNT(DISTINCT c.id) AS cnt
                    FROM gibdd_cards c
                    WHERE c.reg_code = %(reg)s
                      AND c.dat_period = ANY(%(dats)s)
                      AND (
                          EXISTS (
                              SELECT 1 FROM gibdd_participants p
                              WHERE p.card_id = c.id
                                AND p.kt_uch ILIKE '%%пешеход%%'
                          )
                          OR c.dtpv ILIKE '%%пешеход%%'
                          OR c.dtpv ILIKE '%%сим%%'
                      )
                """, {"reg": reg_code, "dats": dat_list})
                r = await cur.fetchone()
                return int(r["cnt"]) if r else 0

    async def _q9_weather() -> list[dict]:
        """Группировка по погоде с тяжестью.
        
        Погода хранится в JSONB-поле spog (массив строк).
        Fallback на raw_payload->'dor_usl'->'spog'.
        """
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    SELECT
                        TRIM(elem) AS weather,
                        COUNT(DISTINCT c.id) AS dtp,
                        COALESCE(SUM(c.pog), 0) AS deaths,
                        COALESCE(SUM(c.ran), 0) AS injured
                    FROM gibdd_cards c,
                         LATERAL jsonb_array_elements_text(
                             COALESCE(
                                 c.spog,
                                 c.raw_payload->'dor_usl'->'spog',
                                 '[]'::jsonb
                             )
                         ) AS elem
                    WHERE c.reg_code = %(reg)s
                      AND c.dat_period = ANY(%(dats)s)
                      AND TRIM(elem) != ''
                    GROUP BY TRIM(elem)
                """, {"reg": reg_code, "dats": dat_list})
                return await cur.fetchall()

    # Запускаем все запросы параллельно
    results = await asyncio.gather(
        _q1_totals(), _q2_type(), _q3_hour(), _q4_weekday(),
        _q5_month(), _q6_road(), _q7_alcohol(), _q8_pedestrian(),
        _q9_weather(),
    )

    (totals,
     type_rows, hour_rows, weekday_rows, month_rows, road_rows,
     alcohol_count, pedestrian_count,
     weather_rows) = results
    total, deaths, injured = totals

    # --- Постобработка: группировки (как в analytics.py) ---
    from analytics import group_dtp_type, group_road_significance

    # Типы ДТП: сырые + сгруппированные
    by_type: dict[str, int] = {}
    by_type_grouped: dict[str, int] = {}
    type_grouped_severity: dict[str, dict[str, int]] = {}

    for r in type_rows:
        raw = str(r["dtpv"]).strip()
        cnt = int(r["dtp"])
        d = int(r["deaths"])
        inj = int(r["injured"])
        if raw:
            by_type[raw] = by_type.get(raw, 0) + cnt
        grouped = group_dtp_type(raw)
        by_type_grouped[grouped] = by_type_grouped.get(grouped, 0) + cnt
        bucket = type_grouped_severity.setdefault(
            grouped, {"dtp": 0, "deaths": 0, "injured": 0}
        )
        bucket["dtp"] += cnt
        bucket["deaths"] += d
        bucket["injured"] += inj

    # Часы
    by_hour: dict[int, int] = {}
    hour_severity: dict[int, dict[str, int]] = {}
    for r in hour_rows:
        h = int(r["hour"])
        cnt = int(r["dtp"])
        by_hour[h] = cnt
        hour_severity[h] = {
            "dtp": cnt, "deaths": int(r["deaths"]), "injured": int(r["injured"])
        }

    # Дни недели
    by_weekday: dict[int, int] = {}
    weekday_severity: dict[int, dict[str, int]] = {}
    for r in weekday_rows:
        wd = int(r["wd"])
        cnt = int(r["dtp"])
        by_weekday[wd] = cnt
        weekday_severity[wd] = {
            "dtp": cnt, "deaths": int(r["deaths"]), "injured": int(r["injured"])
        }

    # Месяцы
    by_month: dict[str, dict[str, int]] = {}
    for r in month_rows:
        m = int(r["month"])
        name = _MONTH_NAMES.get(m)
        if name:
            bucket = by_month.setdefault(name, {"dtp": 0, "deaths": 0, "injured": 0})
            bucket["dtp"] += int(r["dtp"])
            bucket["deaths"] += int(r["deaths"])
            bucket["injured"] += int(r["injured"])

    # Дороги
    by_road: dict[str, int] = {}
    road_significance_severity: dict[str, dict[str, int]] = {}
    for r in road_rows:
        road = str(r["dor"] or "").strip()
        dor_z = str(r["dor_z"] or "").strip()
        cnt = int(r["dtp"])
        if road:
            by_road[road] = by_road.get(road, 0) + cnt
        sig = group_road_significance(dor_z)
        bucket = road_significance_severity.setdefault(
            sig, {"dtp": 0, "deaths": 0, "injured": 0}
        )
        bucket["dtp"] += cnt
        bucket["deaths"] += int(r["deaths"])
        bucket["injured"] += int(r["injured"])

    # Погода
    by_weather: dict[str, int] = {}
    weather_severity: dict[str, dict[str, int]] = {}
    for r in weather_rows:
        w = str(r["weather"]).strip()
        cnt = int(r["dtp"])
        if w:
            by_weather[w] = by_weather.get(w, 0) + cnt
            weather_severity[w] = {
                "dtp": cnt, "deaths": int(r["deaths"]), "injured": int(r["injured"])
            }

    # Сводные показатели на 100 ДТП
    deaths_per_100 = round(deaths / total * 100, 1) if total > 0 else 0
    injured_per_100 = round(injured / total * 100, 1) if total > 0 else 0

    return {
        "total": total,
        "deaths": deaths,
        "injured": injured,
        "alcohol": alcohol_count,
        "pedestrians": pedestrian_count,
        "deaths_per_100": deaths_per_100,
        "injured_per_100": injured_per_100,
        "by_weekday": by_weekday,
        "by_hour": by_hour,
        "by_type": by_type,
        "by_type_grouped": by_type_grouped,
        "by_weather": by_weather,
        "by_road": by_road,
        "by_month": by_month,
        "by_weekday_severity": {
            str(k): v for k, v in weekday_severity.items()
        },
        "by_hour_severity": {
            str(k): v for k, v in hour_severity.items()
        },
        "by_type_grouped_severity": type_grouped_severity,
        "by_weather_severity": weather_severity,
        "by_road_significance": road_significance_severity,
    }
