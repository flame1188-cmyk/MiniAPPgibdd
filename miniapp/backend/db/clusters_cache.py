"""
Кэш очагов концентрации ДТП в PostgreSQL (Этап 4).

По аналогии с cards_cache.py — персистентное SQL-хранилище для
финального результата расчёта очагов (clusters_state.result).

Зачем:
  Расчёт очагов — длительная операция (15-30 сек):
  OSM Overpass + классификация + кластеризация + динамика vs АППГ.
  Если два пользователя запускают очаги по одному и тому же
  региону+периоду — второй сейчас ждёт зря, результат идентичен.

  Кэш позволяет переиспользовать результат между:
  - разными пользователями
  - разными сессиями одного пользователя
  - перезапусками приложения

Ключ кэша:
  (reg_code, current_dat_hash, prev_dat_hash)

  где dat_hash = MD5 от отсортированного списка "m.YYYY" дат.
  Сортировка гарантирует стабильный ключ независимо от порядка месяцев.

  prev_dat_hash = NULL если АППГ не используется.

Что кэшируется:
  ТОЛЬКО финальный сериализованный result (clusters_state.result) —
  словарь с clusters/preclusters/dynamics_summary/... Размер 50-200 KB.

  raw_clusters (с координатами всех карточек) НЕ кэшируются —
  они нужны только для Excel-выгрузки и продвинутой карты,
  их можно пересчитать из task.cards + result.

TTL:
  По умолчанию 6 часов (21600 сек) — очаги стабильнее карточек,
  данные ГИБДД для закрытых периодов уже не меняются.
  Настраивается через env CLUSTERS_CACHE_TTL_SECONDS.

Fallback:
  Если БД недоступна — все операции no-op, расчёт идёт как раньше
  (без кэша, in-memory only).
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, List, Optional, Tuple

from psycopg.types.json import Json

from .connection import get_pool, is_db_ready

logger = logging.getLogger(__name__)

# TTL берётся из env CLUSTERS_CACHE_TTL_SECONDS (по умолчанию 21600 = 6 часов).
# См. config.py → раздел «PostgreSQL-кэш (Этап 3+)».
try:
    from config import CLUSTERS_CACHE_TTL_SECONDS
    DEFAULT_TTL_SECONDS = CLUSTERS_CACHE_TTL_SECONDS
except Exception:
    DEFAULT_TTL_SECONDS = 21600

logger.info(
    f"clusters_cache: TTL={DEFAULT_TTL_SECONDS}s "
    f"(env CLUSTERS_CACHE_TTL_SECONDS)"
)


# ====================================================================
# Хэширование ключа
# ====================================================================
def _make_dat_hash(dat_list: List[str]) -> str:
    """
    Вычисляет MD5-хэш от отсортированного списка дат.

    Сортировка гарантирует стабильный ключ независимо от порядка месяцев.
    Пример: ["1.2026", "2.2026"] → MD5("1.2026,2.2026")
            ["2.2026", "1.2026"] → MD5("1.2026,2.2026")  (та же запись)
    """
    if not dat_list:
        return ""
    sorted_dats = sorted(dat_list)
    raw = ",".join(sorted_dats)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _make_prev_dat_hash(prev_dat_list: Optional[List[str]]) -> Optional[str]:
    """
    Хэш для прошлого периода. Возвращает None если АППГ не используется.
    """
    if not prev_dat_list:
        return None
    return _make_dat_hash(prev_dat_list)


def _compute_prev_dat_list(dat_list: List[str]) -> List[str]:
    """
    Вычисляет dat_list прошлого года: ['1.2026', ...] → ['1.2025', ...]
    (вспомогательная функция для согласованности с ensure_prev_cards).
    """
    prev = []
    for dat in dat_list:
        try:
            m, y = dat.split(".")
            prev.append(f"{m}.{int(y) - 1}")
        except Exception:
            continue
    return prev


# ====================================================================
# GET — чтение из кэша
# ====================================================================
async def get_cached_clusters(
    reg_code: str,
    current_dat_list: List[str],
    prev_dat_list: Optional[List[str]] = None,
) -> Optional[dict]:
    """
    Возвращает сохранённый result из БД или None, если записи нет / протухла.

    Что вернётся:
        {
            "total_clusters": int,
            "total_lost": int,
            "total_prev_matched": int,
            "total_preclusters": int,
            "current_total_dtp": int,
            "current_deaths": int,
            "current_injured": int,
            "dynamics": {...},
            "clusters": [...],
            "preclusters": [...],
            "has_prev_data": bool,
            "prev_label": str | None,
            "current_label": str,
            "region_name": str,
        }

    Это именно тот dict, который кладётся в task.clusters_state.result.
    """
    if not current_dat_list:
        return None

    if not is_db_ready():
        return None

    pool = get_pool()
    if pool is None:
        return None

    current_hash = _make_dat_hash(current_dat_list)
    prev_hash = _make_prev_dat_hash(prev_dat_list)

    try:
        async with pool.connection() as conn:
            # Запрос с учётом NULL prev_hash: если prev_dat_list=None,
            # ищем запись где prev_dat_hash IS NULL.
            if prev_hash is None:
                cur = await conn.execute(
                    """
                    SELECT payload, total_clusters, total_preclusters,
                           has_prev_data
                    FROM clusters_cache
                    WHERE reg_code = %(reg)s
                      AND current_dat_hash = %(curr)s
                      AND prev_dat_hash IS NULL
                      AND expires_at > NOW()
                    """,
                    params={"reg": reg_code, "curr": current_hash},
                    prepare=False,
                )
            else:
                cur = await conn.execute(
                    """
                    SELECT payload, total_clusters, total_preclusters,
                           has_prev_data
                    FROM clusters_cache
                    WHERE reg_code = %(reg)s
                      AND current_dat_hash = %(curr)s
                      AND prev_dat_hash = %(prev)s
                      AND expires_at > NOW()
                    """,
                    params={
                        "reg": reg_code,
                        "curr": current_hash,
                        "prev": prev_hash,
                    },
                    prepare=False,
                )
            row = await cur.fetchone()

        if row is None:
            return None

        payload = row["payload"]
        if not payload:
            return None

        logger.info(
            f"clusters_cache: HIT reg={reg_code} "
            f"curr={current_hash[:8]}.. prev={prev_hash[:8] if prev_hash else 'none'}.. "
            f"({row['total_clusters']} очагов, "
            f"{row['total_preclusters']} предочагов)"
        )
        return dict(payload)

    except Exception as exc:
        logger.warning(
            f"clusters_cache: get_cached_clusters failed (reg={reg_code}): {exc}"
        )
        return None


# ====================================================================
# PUT — сохранение в кэш
# ====================================================================
async def put_cached_clusters(
    reg_code: str,
    current_dat_list: List[str],
    prev_dat_list: Optional[List[str]],
    result: dict,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> None:
    """
    Сохраняет result в БД (upsert: INSERT ... ON CONFLICT DO UPDATE).

    result — это task.clusters_state.result (полный сериализованный dict).
    """
    if not current_dat_list or not result:
        return

    if not is_db_ready():
        return

    pool = get_pool()
    if pool is None:
        return

    current_hash = _make_dat_hash(current_dat_list)
    prev_hash = _make_prev_dat_hash(prev_dat_list)

    # Извлекаем сводные метрики для быстрой диагностики в /health/db/clusters
    total_clusters = int(result.get("total_clusters", 0) or 0)
    total_preclusters = int(result.get("total_preclusters", 0) or 0)
    has_prev_data = bool(result.get("has_prev_data", False))
    current_label = str(result.get("current_label", "") or "")
    prev_label = result.get("prev_label")
    region_name = str(result.get("region_name", "") or "")

    try:
        async with pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO clusters_cache (
                    reg_code, current_dat_hash, prev_dat_hash,
                    current_dat_list, prev_dat_list,
                    payload,
                    total_clusters, total_preclusters, has_prev_data,
                    current_label, prev_label, region_name,
                    created_at, expires_at
                ) VALUES (
                    %(reg)s, %(curr)s, %(prev)s,
                    %(curr_list)s, %(prev_list)s,
                    %(payload)s,
                    %(tc)s, %(tpc)s, %(hpd)s,
                    %(cl)s, %(pl)s, %(rn)s,
                    NOW(),
                    NOW() + (%(ttl)s || ' seconds')::INTERVAL
                )
                ON CONFLICT (reg_code, current_dat_hash,
                             COALESCE(prev_dat_hash, ''::text))
                DO UPDATE SET
                    current_dat_list = EXCLUDED.current_dat_list,
                    prev_dat_list = EXCLUDED.prev_dat_list,
                    payload = EXCLUDED.payload,
                    total_clusters = EXCLUDED.total_clusters,
                    total_preclusters = EXCLUDED.total_preclusters,
                    has_prev_data = EXCLUDED.has_prev_data,
                    current_label = EXCLUDED.current_label,
                    prev_label = EXCLUDED.prev_label,
                    region_name = EXCLUDED.region_name,
                    created_at = NOW(),
                    expires_at = NOW() + (%(ttl)s || ' seconds')::INTERVAL
                """,
                params={
                    "reg": reg_code,
                    "curr": current_hash,
                    "prev": prev_hash,
                    "curr_list": Json(current_dat_list),
                    "prev_list": Json(prev_dat_list) if prev_dat_list else None,
                    "payload": Json(result),
                    "tc": total_clusters,
                    "tpc": total_preclusters,
                    "hpd": has_prev_data,
                    "cl": current_label,
                    "pl": prev_label,
                    "rn": region_name,
                    "ttl": str(ttl_seconds),
                },
            )
            await conn.commit()

        logger.info(
            f"clusters_cache: PUT reg={reg_code} "
            f"curr={current_hash[:8]}.. prev={prev_hash[:8] if prev_hash else 'none'}.. "
            f"({total_clusters} очагов, {total_preclusters} предочагов, "
            f"TTL={ttl_seconds}s)"
        )

    except Exception as exc:
        logger.warning(
            f"clusters_cache: put_cached_clusters failed (reg={reg_code}): {exc}"
        )


# ====================================================================
# INVALIDATE BY REGION
# ====================================================================
async def invalidate_region(reg_code: str) -> int:
    """
    Удаляет ВСЕ записи кэша очагов для заданного региона.

    Используется когда данные ГИБДД по региону обновились и нужно
    форсировать перерасчёт.

    Возвращает количество удалённых строк.
    """
    if not is_db_ready():
        return 0

    pool = get_pool()
    if pool is None:
        return 0

    try:
        async with pool.connection() as conn:
            cur = await conn.execute(
                "DELETE FROM clusters_cache WHERE reg_code = %(reg)s",
                params={"reg": reg_code},
                prepare=False,
            )
            await conn.commit()
            removed = cur.rowcount or 0

        if removed > 0:
            logger.info(
                f"clusters_cache: invalidate_region({reg_code}) — "
                f"удалено {removed} записей"
            )
        return removed

    except Exception as exc:
        logger.warning(
            f"clusters_cache: invalidate_region failed (reg={reg_code}): {exc}"
        )
        return 0


# ====================================================================
# CLEANUP OLD — удаление протухших записей
# ====================================================================
async def cleanup_old_clusters() -> int:
    """
    Физически удаляет протухшие записи (expires_at < NOW()).

    Вызывается из background-задачи main.py (_cleanup_loop).
    """
    if not is_db_ready():
        return 0

    pool = get_pool()
    if pool is None:
        return 0

    try:
        async with pool.connection() as conn:
            cur = await conn.execute(
                "DELETE FROM clusters_cache WHERE expires_at < NOW()",
                prepare=False,
            )
            await conn.commit()
            removed = cur.rowcount or 0

        if removed > 0:
            logger.info(
                f"clusters_cache: cleanup_old_clusters — удалено {removed} "
                f"протухших записей"
            )
        return removed

    except Exception as exc:
        logger.warning(f"clusters_cache: cleanup_old_clusters failed: {exc}")
        return 0


# ====================================================================
# STATS — для диагностики (/health/db/clusters)
# ====================================================================
async def get_cache_stats() -> dict:
    """
    Возвращает статистику кэша очагов для диагностики.
    """
    if not is_db_ready():
        return {
            "configured": False,
            "ready": False,
            "reason": "DATABASE_URL not set or pool not ready",
        }

    pool = get_pool()
    if pool is None:
        return {
            "configured": True,
            "ready": False,
            "reason": "pool is None",
        }

    try:
        async with pool.connection() as conn:
            # Общая статистика
            cur = await conn.execute(
                """
                SELECT
                    COUNT(*) AS total_entries,
                    COUNT(*) FILTER (WHERE expires_at > NOW()) AS valid_entries,
                    COALESCE(SUM(total_clusters) FILTER (WHERE expires_at > NOW()), 0) AS total_clusters_cached,
                    COALESCE(SUM(total_preclusters) FILTER (WHERE expires_at > NOW()), 0) AS total_preclusters_cached,
                    COUNT(*) FILTER (WHERE expires_at > NOW() AND has_prev_data) AS entries_with_prev,
                    COUNT(DISTINCT reg_code) FILTER (WHERE expires_at > NOW()) AS regions_cached,
                    MIN(expires_at) FILTER (WHERE expires_at > NOW()) AS oldest_expiry,
                    MAX(expires_at) FILTER (WHERE expires_at > NOW()) AS newest_expiry
                FROM clusters_cache
                """,
                prepare=False,
            )
            row = await cur.fetchone()

            # Top-5 регионов по размеру кэша
            cur = await conn.execute(
                """
                SELECT reg_code,
                       COUNT(*) AS entries,
                       SUM(total_clusters) AS clusters,
                       SUM(total_preclusters) AS preclusters,
                       MAX(region_name) AS region_name
                FROM clusters_cache
                WHERE expires_at > NOW()
                GROUP BY reg_code
                ORDER BY clusters DESC NULLS LAST
                LIMIT 5
                """,
                prepare=False,
            )
            top_regions = await cur.fetchall()

        return {
            "configured": True,
            "ready": True,
            "total_entries": row["total_entries"] if row else 0,
            "valid_entries": row["valid_entries"] if row else 0,
            "total_clusters_cached": row["total_clusters_cached"] if row else 0,
            "total_preclusters_cached": row["total_preclusters_cached"] if row else 0,
            "entries_with_prev": row["entries_with_prev"] if row else 0,
            "regions_cached": row["regions_cached"] if row else 0,
            "oldest_expiry": row["oldest_expiry"].isoformat()
            if row and row["oldest_expiry"]
            else None,
            "newest_expiry": row["newest_expiry"].isoformat()
            if row and row["newest_expiry"]
            else None,
            "top_regions": [
                {
                    "reg_code": r["reg_code"],
                    "entries": r["entries"],
                    "clusters": r["clusters"],
                    "preclusters": r["preclusters"],
                    "region_name": r["region_name"],
                }
                for r in (top_regions or [])
            ],
        }

    except Exception as exc:
        return {
            "configured": True,
            "ready": False,
            "reason": f"stats query failed: {exc}",
        }
