"""
archive.py — чтение карточек ДТП из постоянного архива PostgreSQL.

Архив (gibdd_cards + gibdd_vehicles + gibdd_participants) — это
предзагруженные данные за 2021-2026 по всем 90 регионам РФ.

Используется как fallback L2.5 в _fetch_cards_for_period:
    L1 (in-memory data_cache) → L2 (dtp_cards_cache JSONB, TTL 7д)
    → L2.5 (gibdd_cards архив, без TTL) → L3 (прямой запрос к API ГИБДД)

Если запрошенные месяцы уже есть в архиве — отдаём их мгновенно,
без обращения к stat.gibdd.ru. Это убирает зависимость от доступности
ГИБДД для исторических периодов.

Возвращает данные в том же формате, что extract_accident_cards() —
list[dict] с ключами empt_number, date_dtp, time, ts_info, uch_info, ...
(сырой JSONB сохранён в gibdd_cards.raw_payload).
"""
from __future__ import annotations

import logging
from typing import Optional

from .connection import get_pool, is_db_ready

logger = logging.getLogger(__name__)


async def get_cards_from_archive(
    reg_code: str,
    dat_list: list[str],
) -> Optional[list[dict]]:
    """
    Читает карточки ДТП из архива gibdd_cards по (reg_code, dat_list).

    Args:
        reg_code: Код региона (например "1146" для Московской обл.)
        dat_list: Список периодов ["1.2026", "2.2026", ...]

    Returns:
        list[dict] с сырыми карточками (raw_payload) в том же формате,
        что extract_accident_cards() — или None, если в архиве нет данных
        хотя бы за один из запрошенных месяцев.
    """
    if not is_db_ready():
        return None

    pool = get_pool()
    if pool is None:
        return None

    if not dat_list:
        return None

    # 1. Проверяем, что ВСЕ запрошенные месяцы загружены в архив.
    # Если хотя бы одного нет — возвращаем None, чтобы вызывающий код
    # мог пойти в API ГИБДД.
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                SELECT dat_period, COUNT(*) AS cards_count
                FROM gibdd_cards
                WHERE reg_code = %(reg)s
                  AND dat_period = ANY(%(dats)s)
                GROUP BY dat_period
            """, {"reg": reg_code, "dats": dat_list})
            rows = await cur.fetchall()

    # 2. Сравниваем список присутствующих месяцев с запрошенным
    found_dats = {r["dat_period"] for r in rows} if rows else set()
    missing_dats = [d for d in dat_list if d not in found_dats]

    if missing_dats:
        logger.debug(
            f"archive miss for reg={reg_code}: missing {missing_dats} "
            f"(found {len(found_dats)}/{len(dat_list)})"
        )
        return None

    # 3. Все месяцы есть — читаем карточки
    # Возвращаем kart_id и empt_number из БД — это важно, чтобы в Excel
    # столбцы «Номер» и «Номер ДТП» были заполнены (см. gibdd_parser.py).
    # raw_payload содержит оригинальный ответ ГИБДД, но без kart_id
    # (он вычисляется только в ETL). Добавляем kart_id/empt_number в карточку
    # после распаковки raw_payload.
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                SELECT id, kart_id, empt_number, raw_payload
                FROM gibdd_cards
                WHERE reg_code = %(reg)s
                  AND dat_period = ANY(%(dats)s)
                ORDER BY date_dtp, time
            """, {"reg": reg_code, "dats": dat_list})
            card_rows = await cur.fetchall()

            if not card_rows:
                return None

            cards: list[dict] = []
            for row in card_rows:
                payload = row["raw_payload"]
                if isinstance(payload, str):
                    import json
                    payload = json.loads(payload)
                if isinstance(payload, dict):
                    # Добавляем kart_id и empt_number из БД (если их нет в raw_payload)
                    # — это вычисленные при ETL значения, оригинальный raw_payload
                    #   из API ГИБДД их не содержит.
                    if row.get("kart_id") and not payload.get("kart_id"):
                        payload["kart_id"] = row["kart_id"]
                    if row.get("empt_number") and not payload.get("empt_number"):
                        payload["empt_number"] = row["empt_number"]
                    cards.append(payload)

            logger.info(
                f"  archive hit: reg={reg_code} dat_list={dat_list} "
                f"→ {len(cards)} карточек"
            )
            return cards


async def get_archive_coverage() -> dict[str, dict[str, int]]:
    """
    Возвращает покрытие архива: {reg_code: {dat_period: count}}.
    Используется для отображения прогресса ETL в админке.
    """
    if not is_db_ready():
        return {}

    pool = get_pool()
    if pool is None:
        return {}

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                SELECT reg_code, dat_period, COUNT(*) AS cnt
                FROM gibdd_cards
                GROUP BY reg_code, dat_period
                ORDER BY reg_code, dat_period
            """)
            rows = await cur.fetchall()

    result: dict[str, dict[str, int]] = {}
    for row in rows:
        result.setdefault(row["reg_code"], {})[row["dat_period"]] = row["cnt"]
    return result


async def count_archive_cards(reg_code: str, dat_list: list[str]) -> int:
    """
    Возвращает количество карточек в архиве для (reg, dat_list).
    Быстрая проверка без загрузки данных — используется в кэше.
    """
    if not is_db_ready():
        return 0

    pool = get_pool()
    if pool is None:
        return 0

    if not dat_list:
        return 0

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                SELECT COUNT(*) AS c
                FROM gibdd_cards
                WHERE reg_code = %(reg)s
                  AND dat_period = ANY(%(dats)s)
            """, {"reg": reg_code, "dats": dat_list})
            row = await cur.fetchone()
            return row["c"] if row else 0
