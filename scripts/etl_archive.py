#!/usr/bin/env python3
"""
ETL: Первоначальная загрузка карточек ДТП из API ГИБДД в постоянный архив.

Архитектура:
  - Один API-вызов = один (регион, месяц) → возвращает все карточки за период
  - Глобальный throttle 1 req/s (унаследован от api_client._throttle)
  - Нормализация: card → gibdd_cards, ts_info → gibdd_vehicles, ts_uch/uch_info → gibdd_participants
  - Resumability: etl_log хранит статус каждого (reg, dat), при перезапуске готовые пропускаются
  - Порядок обхода: по месяцу, затем по региону — даёт равномерное покрытие при прерывании

Запуск:
  # Полная загрузка 2021-2026 × 90 регионов
  python3 scripts/etl_archive.py --start-year 2021 --end-year 2026

  # Инкрементальное обновление (последний завершённый месяц по всем регионам)
  python3 scripts/etl_archive.py --incremental

  # Тест на одном регионе
  python3 scripts/etl_archive.py --reg 1199 --start-year 2026 --end-year 2026
"""
import asyncio
import json
import logging
import os
import sys
import time as _time
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

# Загружаем .env из проекта
ENV_PATH = Path("/home/z/my-project/gibdd-bot/.env")
for line in ENV_PATH.read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ[k.strip()] = v.strip()

# Импорты проекта
# SCRIPT_DIR позволяет импортировать kart_id_utils.py рядом с этим скриптом
# (там же, где он лежит в репозитории MiniAPPgibdd/scripts/).
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
# Совместимость со старой раскладкой /home/z/my-project/gibdd-bot/ — если репозиторий
# лежит именно там, оставляем PROJECT_ROOT для доступа к api_client/config бота.
PROJECT_ROOT = Path("/home/z/my-project/gibdd-bot")
if PROJECT_ROOT.is_dir():
    sys.path.insert(0, str(PROJECT_ROOT))
    sys.path.insert(0, str(PROJECT_ROOT / "miniapp" / "backend"))

import httpx
import psycopg
from psycopg.rows import dict_row

# ─────────────────────────────────────────────────────────────────────────
# HTTP-клиент к GIBDD (свой, чтобы не тянуть зависимость от api_client.config)
# ─────────────────────────────────────────────────────────────────────────
GIBDD_BASE_URL = "http://xn--80a7adb.xn--90adear.xn--p1ai"
MIN_REQUEST_INTERVAL = 1.0  # секунд между запросами (GIBDD банит при >1 req/s)
_last_request_time: float = 0.0
_throttle_lock = asyncio.Lock()

# Разделяемый HTTP-клиент (keep-alive)
_shared_client: httpx.AsyncClient | None = None


async def get_client() -> httpx.AsyncClient:
    global _shared_client
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=60, read=120, write=30, pool=30),
            limits=httpx.Limits(max_connections=5, max_keepalive_connections=3, keepalive_expiry=120),
            http2=False,
        )
    return _shared_client


async def _throttle():
    """Глобальный throttle: не более 1 запроса в секунду."""
    global _last_request_time
    async with _throttle_lock:
        now = _time.monotonic()
        elapsed = now - _last_request_time
        if elapsed < MIN_REQUEST_INTERVAL:
            wait = MIN_REQUEST_INTERVAL - elapsed
            await asyncio.sleep(wait)
        _last_request_time = _time.monotonic()


async def fetch_dtp_data(dat: str, reg: str, pok: str = "1") -> dict[str, Any]:
    """GET /opendataapi/v1/kartdtp/rows?pok=1&dat=7.2026&reg=1146 → JSON."""
    if reg == "1100":  # РФ целиком — не валиден
        raise ValueError("reg=1100 (Russian Federation whole) is not allowed")

    url = f"{GIBDD_BASE_URL}/opendataapi/v1/kartdtp/rows"
    params = {"pok": pok, "dat": dat, "reg": reg}

    await _throttle()
    client = await get_client()
    response = await client.get(url, params=params)
    response.raise_for_status()
    data = response.json()
    if data.get("status") != 200:
        raise ValueError(f"GIBDD API returned status={data.get('status')}")
    return data


def extract_accident_cards(api_response: dict) -> list[dict[str, Any]]:
    """Извлекает плоский список карточек из ответа API."""
    cards: list[dict[str, Any]] = []
    results = api_response.get("results", {})

    # results может быть dict или list (в разных версиях API)
    region_list = []
    if isinstance(results, dict):
        region_list = results.get("region_list", [])
    elif isinstance(results, list):
        # в случае dict-формат, results — list с одним элементом
        for r in results:
            if isinstance(r, dict) and "region_list" in r:
                region_list.extend(r["region_list"])

    for region in region_list:
        for pok in region.get("pok_list", []):
            for result in pok.get("result", []):
                cards.extend(result.get("dtpcardlist", {}).get("info_dtp", []))
    return cards

# ─────────────────────────────────────────────────────────────────────────
# Логирование
# ─────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("/home/z/my-project/scripts/etl_archive.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("etl")

DB_URL = os.environ["DATABASE_URL"]

# ─────────────────────────────────────────────────────────────────────────
# Ретраи (терпеливые, для ETL)
# ─────────────────────────────────────────────────────────────────────────
RETRY_DELAYS = [10, 30, 60, 120, 300]  # секунды


# ─────────────────────────────────────────────────────────────────────────
# DB helpers
# ─────────────────────────────────────────────────────────────────────────
async def db_fetch_regions(pool) -> list[tuple[str, str]]:
    """Возвращает [(reg_code, reg_name), ...] из gibdd_regions."""
    # Используем отдельное соединение без dict_row, чтобы получить кортежи
    async with await psycopg.AsyncConnection.connect(DB_URL) as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT reg_code, reg_name FROM gibdd_regions ORDER BY reg_code")
            rows = await cur.fetchall()
            return [(r[0], r[1]) for r in rows]


async def etl_log_status_map(pool, tasks: list[tuple[str, str, str]]) -> dict[tuple[str, str], str]:
    """Возвращает {(reg_code, dat): status} одним батч-запросом.
    Быстрее, чем N отдельных SELECT'ов для больших списков задач.
    """
    if not tasks:
        return {}
    pairs = [(t[0], t[2]) for t in tasks]
    result: dict[tuple[str, str], str] = {}
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            # Используем ANY(%s) для массива кортежей
            await cur.execute("""
                SELECT reg_code, dat_period, status
                FROM etl_log
                WHERE (reg_code, dat_period) IN (
                    SELECT r::text, d::text FROM (VALUES %s) AS t(r, d)
                )
            """, pairs) if False else None
            # psycopg не умеет VALUES %s напрямую — используем unnest
            regs = [p[0] for p in pairs]
            dats = [p[1] for p in pairs]
            await cur.execute("""
                SELECT e.reg_code, e.dat_period, e.status
                FROM etl_log e
                JOIN unnest(%s::text[], %s::text[]) AS x(reg_code, dat_period)
                  ON x.reg_code = e.reg_code AND x.dat_period = e.dat_period
            """, (regs, dats))
            rows = await cur.fetchall()
            for r in rows:
                result[(r["reg_code"], r["dat_period"])] = r["status"]
    return result


async def etl_log_status(pool, reg_code: str, dat: str) -> str | None:
    """Возвращает текущий статус (reg, dat) из etl_log или None."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT status FROM etl_log WHERE reg_code=%s AND dat_period=%s",
                (reg_code, dat),
            )
            row = await cur.fetchone()
            return row["status"] if row else None


async def etl_log_upsert(pool, reg_code: str, dat: str, status: str, cards_count: int = 0, error: str | None = None):
    """Обновляет статус в etl_log."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                INSERT INTO etl_log (reg_code, dat_period, status, cards_count, error, attempts, fetched_at)
                VALUES (%s, %s, %s, %s, %s, 1, CASE WHEN %s = 'done' THEN NOW() ELSE NULL END)
                ON CONFLICT (reg_code, dat_period) DO UPDATE SET
                    status = EXCLUDED.status,
                    cards_count = EXCLUDED.cards_count,
                    error = EXCLUDED.error,
                    attempts = etl_log.attempts + 1,
                    fetched_at = CASE WHEN EXCLUDED.status = 'done' THEN NOW() ELSE etl_log.fetched_at END
            """, (reg_code, dat, status, cards_count, error, status))


# ─────────────────────────────────────────────────────────────────────────
# Нормализация одной карточки → батчи для execute_values
# ─────────────────────────────────────────────────────────────────────────
from psycopg.types.json import Jsonb


def parse_date_dtp(s: str | None) -> date | None:
    """Парсит '31.07.2026' → date(2026, 7, 31)."""
    if not s:
        return None
    try:
        return datetime.strptime(s, "%d.%m.%Y").date()
    except Exception:
        return None


def parse_time(s: str | None):
    """Парсит '11:15' → time(11, 15)."""
    if not s:
        return None
    try:
        from datetime import time as dt_time
        h, m = s.split(":")
        return dt_time(int(h), int(m))
    except Exception:
        return None


def parse_int(s: Any, default: int = 0) -> int:
    if s is None or s == "":
        return default
    try:
        return int(s)
    except (ValueError, TypeError):
        return default


def parse_smallint(s: Any) -> int | None:
    """Возвращает int или None (для NULLABLE SMALLINT)."""
    if s is None or s == "":
        return None
    try:
        return int(s)
    except (ValueError, TypeError):
        return None


def parse_float(s: Any) -> float | None:
    if s is None or s == "":
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


# ─────────────────────────────────────────────────────────────────────────
# Формирование kart_id (17 цифр): region(2) + year(2) + month(2) + day(2) + empt_number(9)
# ─────────────────────────────────────────────────────────────────────────
# Реализация в kart_id_utils.py — там же, где dry-run тесты и док.
# kart_id_utils.py лежит рядом (scripts/) — это же модуль использует и
# miniapp/backend/db/kart_id_utils.py в рантайме (archive.py, api_client.py).
from kart_id_utils import build_kart_id


# SQL для batch-insert'а (одна VALUES на карточку)
# ON CONFLICT DO NOTHING: при коллизии kart_id вторая карта НЕ затирает первую.
# RETURNING вернёт только успешно вставленные — остальные попадут в collisions.
INSERT_CARD_SQL = """
INSERT INTO gibdd_cards (
    kart_id, empt_number, reg_code, dat_period, date_dtp, time,
    coord_w, coord_l, dtpv, k_ts, k_uch, pog, ran, s_dtp,
    district, house, km, m, np, street, dor, dor_z, dor_k, k_ul,
    s_pch, osv, chom, sdor, obj_dtp, ndu, factor, spog,
    raw_payload, source, fetched_at
) VALUES %s
ON CONFLICT (kart_id) DO NOTHING
RETURNING id, kart_id
"""

INSERT_COLLISION_SQL = """
INSERT INTO gibdd_cards_collisions (
    kart_id, reg_code, dat_period, date_dtp, empt_number,
    coord_w, coord_l, pog, ran, raw_payload, conflict_with
) VALUES %s
"""

INSERT_VEHICLE_SQL = """
INSERT INTO gibdd_vehicles (
    card_id, n_ts, ts_s, t_ts, m_ts, marka_ts, color, m_pov, t_n, r_rul, g_v, o_pf
) VALUES %s
RETURNING id, card_id, n_ts
"""

INSERT_PARTICIPANT_SQL = """
INSERT INTO gibdd_participants (
    card_id, vehicle_id, n_uch, kt_uch, s_sm, pol, s_t, npdd, sop_npdd,
    safety_belt, s_seat_group, alco, v_st
) VALUES %s
"""


async def batch_insert_cards(
    cur, cards: list[dict], reg_code: str, dat: str
) -> tuple[dict[str, int], dict[int, tuple[str, int]], int]:
    """
    Batch INSERT карточек с новой логикой kart_id = region(2)+year(2)+month(2)+day(2)+empt_number(9) = 17 цифр.

    Returns:
        (kart_to_id, index_to_info, collisions_count)
        - kart_to_id: {kart_id: card_id} для успешно вставленных
        - index_to_info: {index_in_cards: (kart_id, card_id)} — для vehicles/participants
        - collisions_count: сколько карт попало в gibdd_cards_collisions
    """
    if not cards:
        return {}, {}, 0

    # 1. Готовим строки для batch INSERT
    rows = []
    kart_id_by_index: dict[int, str] = {}
    skipped = 0
    for i, card in enumerate(cards):
        kart_id, empt_number = build_kart_id(card, reg_code)
        if not kart_id:
            # Нет empt_number или date_dtp — пропускаем
            skipped += 1
            continue
        kart_id_by_index[i] = kart_id
        dor_usl = card.get("dor_usl") or {}
        rows.append((
            kart_id,
            empt_number,                       # новый столбец empt_number
            reg_code,
            dat,
            parse_date_dtp(card.get("date_dtp")),
            parse_time(card.get("time")),
            parse_float(card.get("coord_w")),
            parse_float(card.get("coord_l")),
            card.get("dtpv"),
            parse_smallint(card.get("k_ts")),
            parse_smallint(card.get("k_uch")),
            parse_int(card.get("pog"), 0),
            parse_int(card.get("ran"), 0),
            card.get("s_dtp"),
            card.get("district"),
            card.get("house"),
            card.get("km"),
            card.get("m"),
            card.get("np"),
            card.get("street"),
            card.get("dor"),
            card.get("dor_z"),
            card.get("dor_k"),
            card.get("k_ul"),
            dor_usl.get("s_pch"),
            dor_usl.get("osv"),
            dor_usl.get("chom"),
            Jsonb(dor_usl.get("sdor", [])),
            Jsonb(dor_usl.get("obj_dtp", [])),
            Jsonb(dor_usl.get("ndu", [])),
            Jsonb(dor_usl.get("factor", [])),
            Jsonb(dor_usl.get("spog", [])),
            Jsonb(card),
            "api",
            datetime.now(),
        ))

    if not rows:
        log.warning(f"  ⚠  Нет валидных карт для вставки (skipped={skipped})")
        return {}, {}, 0

    # 2. Batch INSERT с ON CONFLICT DO NOTHING
    # 35 плейсхолдеров на карту (kart_id, empt_number, reg_code, dat_period, date_dtp, time,
    # coord_w, coord_l, dtpv, k_ts, k_uch, pog, ran, s_dtp, district, house, km, m, np,
    # street, dor, dor_z, dor_k, k_ul, s_pch, osv, chom, sdor, obj_dtp, ndu, factor, spog,
    # raw_payload, source, fetched_at) = 35
    placeholder = "(" + ",".join(["%s"] * 35) + ")"
    placeholders = ",".join([placeholder] * len(rows))
    sql = INSERT_CARD_SQL.replace("VALUES %s", f"VALUES {placeholders}")
    flat_args = [v for row in rows for v in row]
    await cur.execute(sql, flat_args)

    # 3. Собираем RETURNING — только успешно вставленные
    inserted_kart_ids = set()
    kart_to_id: dict[str, int] = {}
    async for r in cur:
        inserted_kart_ids.add(r["kart_id"])
        kart_to_id[r["kart_id"]] = r["id"]

    # 4. Находим коллизии: kart_id, которые были в исходном батче, но НЕ вставились
    # Это значит, что kart_id уже существует в БД (другая карта заняла его раньше)
    collision_rows = []
    for i, card in enumerate(cards):
        kart_id = kart_id_by_index.get(i)
        if kart_id is None or kart_id in inserted_kart_ids:
            continue
        # Коллизия!
        empt_number = str(card.get("empt_number") or card.get("kart_id") or "")
        dor_usl = card.get("dor_usl") or {}
        collision_rows.append((
            kart_id,
            reg_code,
            dat,
            parse_date_dtp(card.get("date_dtp")),
            empt_number,
            parse_float(card.get("coord_w")),
            parse_float(card.get("coord_l")),
            parse_int(card.get("pog"), 0),
            parse_int(card.get("ran"), 0),
            Jsonb(card),
            None,  # conflict_with — заполним ниже, после SELECT
        ))

    # 5. Если есть коллизии — SELECT существующих card_id и INSERT в collisions
    collisions_count = 0
    if collision_rows:
        # Получаем kart_id, по которым есть коллизии
        collision_kart_ids = list({row[0] for row in collision_rows})

        # SELECT существующих card_id для этих kart_id
        select_sql = (
            "SELECT id, kart_id FROM gibdd_cards WHERE kart_id = ANY(%s)"
        )
        await cur.execute(select_sql, (collision_kart_ids,))
        existing_map: dict[str, int] = {}
        async for r in cur:
            existing_map[r["kart_id"]] = r["id"]

        # Заполняем conflict_with
        final_collision_rows = []
        for row in collision_rows:
            kart_id = row[0]
            conflict_with = existing_map.get(kart_id)
            final_collision_rows.append((
                kart_id,
                row[1],  # reg_code
                row[2],  # dat_period
                row[3],  # date_dtp
                row[4],  # empt_number
                row[5],  # coord_w
                row[6],  # coord_l
                row[7],  # pog
                row[8],  # ran
                row[9],  # raw_payload
                conflict_with,
            ))

        # Batch INSERT в collisions
        coll_placeholder = "(" + ",".join(["%s"] * 11) + ")"
        coll_placeholders = ",".join([coll_placeholder] * len(final_collision_rows))
        coll_sql = INSERT_COLLISION_SQL.replace("VALUES %s", f"VALUES {coll_placeholders}")
        coll_flat_args = [v for row in final_collision_rows for v in row]
        await cur.execute(coll_sql, coll_flat_args)
        collisions_count = len(final_collision_rows)

        if collisions_count:
            log.info(
                f"  ⚠  Коллизий kart_id: {collisions_count} (записаны в gibdd_cards_collisions)"
            )

    # 6. Готовим index_to_info для vehicles/participants
    index_to_info: dict[int, tuple[str, int]] = {}
    for i, card in enumerate(cards):
        kart_id = kart_id_by_index.get(i)
        if kart_id and kart_id in kart_to_id:
            index_to_info[i] = (kart_id, kart_to_id[kart_id])

    if skipped:
        log.debug(f"  ℹ  Пропущено карт без empt_number/date_dtp: {skipped}")

    return kart_to_id, index_to_info, collisions_count


async def batch_insert_vehicles(
    cur,
    cards: list[dict],
    index_to_info: dict[int, tuple[str, int]],
) -> dict[tuple[str, str], int]:
    """Batch INSERT ТС. Возвращает {(kart_id, n_ts): vehicle_id}."""
    rows = []
    for i, card in enumerate(cards):
        info = index_to_info.get(i)
        if not info:
            continue
        kart_id, card_id = info
        for ts in card.get("ts_info", []):
            n_ts_str = str(ts.get("n_ts") or "")
            rows.append((
                card_id,
                parse_smallint(ts.get("n_ts")),
                ts.get("ts_s"),
                ts.get("t_ts"),
                ts.get("m_ts"),
                ts.get("marka_ts"),
                ts.get("color"),
                ts.get("m_pov"),
                ts.get("t_n"),
                ts.get("r_rul"),
                parse_smallint(ts.get("g_v")),
                ts.get("o_pf"),
            ))

    if not rows:
        return {}

    placeholder = "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
    placeholders = ",".join([placeholder] * len(rows))
    sql = INSERT_VEHICLE_SQL.replace("VALUES %s", f"VALUES {placeholders}")
    flat_args = [v for row in rows for v in row]
    await cur.execute(sql, flat_args)

    # Строим обратный map card_id → kart_id (для RETURNING)
    card_id_to_kart = {cid: k for (k, cid) in index_to_info.values()}

    veh_map = {}
    async for r in cur:
        card_id = r["card_id"]
        kart_id = card_id_to_kart.get(card_id)
        if kart_id:
            veh_map[(kart_id, str(r["n_ts"] or ""))] = r["id"]
    return veh_map


async def batch_insert_participants(
    cur,
    cards: list[dict],
    index_to_info: dict[int, tuple[str, int]],
    veh_map: dict[tuple[str, str], int],
):
    """Batch INSERT участников."""
    rows = []
    for i, card in enumerate(cards):
        info = index_to_info.get(i)
        if not info:
            continue
        kart_id, card_id = info

        # Участники в ТС
        for ts in card.get("ts_info", []):
            n_ts_str = str(ts.get("n_ts") or "")
            vehicle_id = veh_map.get((kart_id, n_ts_str))
            for u in ts.get("ts_uch", []):
                rows.append((
                    card_id,
                    vehicle_id,
                    parse_smallint(u.get("n_uch")),
                    u.get("kt_uch"),
                    u.get("s_sm"),
                    u.get("pol"),
                    u.get("s_t"),
                    Jsonb(u.get("npdd", [])),
                    Jsonb(u.get("sop_npdd", [])),
                    u.get("safety_belt"),
                    u.get("s_seat_group"),
                    u.get("alco"),
                    parse_smallint(u.get("v_st")),
                ))

        # Участники без ТС (пешеходы, велосипедисты)
        for u in card.get("uch_info", []):
            rows.append((
                card_id,
                None,
                parse_smallint(u.get("n_uch")),
                u.get("kt_uch"),
                u.get("s_sm"),
                u.get("pol"),
                u.get("s_t"),
                Jsonb(u.get("npdd", [])),
                Jsonb(u.get("sop_npdd", [])),
                u.get("safety_belt"),
                u.get("s_seat_group"),
                u.get("alco"),
                parse_smallint(u.get("v_st")),
            ))

    if not rows:
        return

    placeholder = "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
    placeholders = ",".join([placeholder] * len(rows))
    sql = INSERT_PARTICIPANT_SQL.replace("VALUES %s", f"VALUES {placeholders}")
    flat_args = [v for row in rows for v in row]
    await cur.execute(sql, flat_args)


# ─────────────────────────────────────────────────────────────────────────
# Основной цикл: загрузка одного (регион, месяц)
# ─────────────────────────────────────────────────────────────────────────
async def fetch_and_store_one(pool, reg_code: str, dat: str) -> int:
    """Загружает один (регион, месяц) и пишет в архив. Возвращает кол-во карточек."""
    # 1. Проверяем etl_log — не загружали ли уже
    status = await etl_log_status(pool, reg_code, dat)
    if status == "done":
        log.debug(f"  ⏭  {reg_code}/{dat} уже загружен, пропускаем")
        return 0

    # 2. Ставим статус fetching
    await etl_log_upsert(pool, reg_code, dat, "fetching")

    try:
        # 3. Загружаем (api_client.fetch_dtp_data уже имеет _throttle внутри)
        response = await fetch_dtp_data(dat=dat, reg=reg_code, pok="1")
        cards = extract_accident_cards(response)
        cards_count = len(cards)

        # 4. Нормализуем и пишем в 3 таблицы одной транзакцией (batch INSERT)
        async with pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    # Batch INSERT всех карточек одним SQL-запросом
                    # Возвращает (kart_to_id, index_to_info, collisions_count)
                    kart_to_id, index_to_info, coll_count = await batch_insert_cards(cur, cards, reg_code, dat)
                    # Batch INSERT всех ТС одним SQL-запросом
                    veh_map = await batch_insert_vehicles(cur, cards, index_to_info)
                    # Batch INSERT всех участников одним SQL-запросом
                    await batch_insert_participants(cur, cards, index_to_info, veh_map)

        # 5. Фиксируем успех
        await etl_log_upsert(pool, reg_code, dat, "done", cards_count=cards_count)
        return cards_count

    except Exception as e:
        await etl_log_upsert(pool, reg_code, dat, "error", error=str(e)[:500])
        raise


async def fetch_with_retries(pool, reg_code: str, dat: str) -> int:
    """Терпеливые ретраи для ETL (до 5 попыток)."""
    last_exc = None
    for attempt, delay in enumerate(RETRY_DELAYS, 1):
        try:
            return await fetch_and_store_one(pool, reg_code, dat)
        except httpx.HTTPStatusError as e:
            last_exc = e
            status = e.response.status_code
            if status == 429 or status >= 500:
                log.warning(f"  ⚠  HTTP {status} на {reg_code}/{dat} (попытка {attempt}/{len(RETRY_DELAYS)}), ждём {delay}s")
                await asyncio.sleep(delay)
            else:
                log.error(f"  ✗  HTTP {status} на {reg_code}/{dat} — не ретраим (4xx)")
                raise
        except (httpx.TimeoutException, httpx.ConnectError, OSError) as e:
            last_exc = e
            log.warning(f"  ⚠  network error на {reg_code}/{dat} (попытка {attempt}/{len(RETRY_DELAYS)}), ждём {delay}s: {e}")
            await asyncio.sleep(delay)
    raise RuntimeError(f"Не удалось загрузить {reg_code}/{dat} после {len(RETRY_DELAYS)} попыток: {last_exc}")


# ─────────────────────────────────────────────────────────────────────────
# Очередь задач
# ─────────────────────────────────────────────────────────────────────────
def build_task_list(regions: list[tuple[str, str]], start_year: int, end_year: int,
                    reg_filter: str | None = None) -> list[tuple[str, str, str]]:
    """Генерирует список (reg_code, reg_name, dat)."""
    tasks = []
    for year in range(start_year, end_year + 1):
        for month in range(1, 13):
            dat = f"{month}.{year}"
            for reg_code, reg_name in regions:
                if reg_filter and reg_code != reg_filter:
                    continue
                tasks.append((reg_code, reg_name, dat))
    # Сортируем по (dat, reg) — равномерное покрытие при прерывании
    tasks.sort(key=lambda t: (t[2], t[0]))
    return tasks


def build_incremental_tasks(regions: list[tuple[str, str]]) -> list[tuple[str, str, str]]:
    """Загружает только последний завершённый месяц по всем регионам."""
    today = date.today()
    first_of_current = today.replace(day=1)
    last_month_date = first_of_current - timedelta(days=1)
    dat = f"{last_month_date.month}.{last_month_date.year}"
    return [(reg_code, reg_name, dat) for reg_code, reg_name in regions]


# ─────────────────────────────────────────────────────────────────────────
# Главная функция
# ─────────────────────────────────────────────────────────────────────────
async def main():
    import argparse
    parser = argparse.ArgumentParser(description="ETL: загрузка карточек ДТП из ГИБДД в архив")
    parser.add_argument("--start-year", type=int, default=2021, help="Стартовый год (по умолчанию 2021)")
    parser.add_argument("--end-year", type=int, default=2026, help="Конечный год (по умолчанию 2026)")
    parser.add_argument("--reg", type=str, default=None, help="Только один регион (код, например 1199)")
    parser.add_argument("--incremental", action="store_true", help="Только последний завершённый месяц")
    parser.add_argument("--dry-run", action="store_true", help="Показать список задач, но не выполнять")
    args = parser.parse_args()

    log.info("=" * 70)
    log.info("ETL: Архив карточек ДТП из ГИБДД")
    log.info("=" * 70)

    # Пул соединений
    from psycopg_pool import AsyncConnectionPool
    pool = AsyncConnectionPool(DB_URL, min_size=2, max_size=5, open=False, kwargs={"row_factory": dict_row})
    await pool.open(wait=True)
    log.info("✓ Пул соединений к БД открыт")

    # Загружаем список регионов из БД
    regions = await db_fetch_regions(pool)
    log.info(f"✓ Регионов в БД: {len(regions)}")

    # Формируем список задач
    if args.incremental:
        tasks = build_incremental_tasks(regions)
        log.info(f"📅 Инкрементальный режим: последний завершённый месяц ({tasks[0][2]})")
    else:
        tasks = build_task_list(regions, args.start_year, args.end_year, args.reg)
        log.info(f"📅 Период: {args.start_year}-{args.end_year}, всего задач: {len(tasks)}")

    if args.dry_run:
        log.info("Dry run — первые 10 задач:")
        for reg_code, reg_name, dat in tasks[:10]:
            log.info(f"  {reg_code} {reg_name}  →  {dat}")
        return

    # Считаем, сколько уже выполнено — одним батч-запросом
    status_map = await etl_log_status_map(pool, tasks)
    already_done = sum(1 for (_, _, dat) in tasks if status_map.get((tasks[0][0], dat)) == "done")
    # Точнее: считаем по (reg, dat) парам
    already_done = sum(1 for (reg, _, dat) in tasks if status_map.get((reg, dat)) == "done")
    log.info(f"✓ Уже загружено (по etl_log): {already_done}/{len(tasks)}")
    log.info(f"🚀 Начинаем загрузку: {len(tasks) - already_done} задач")
    log.info(f"⏱  Ожидаемое время: ~{(len(tasks) - already_done) * 1.5 / 60:.0f} мин (1.5 сек/задача с throttle)")

    # Цикл загрузки
    total = len(tasks)
    done = already_done
    failed = []
    start_time = time.monotonic()
    total_cards = 0

    for i, (reg_code, reg_name, dat) in enumerate(tasks, 1):
        # Пропускаем уже загруженные (используем кэшированную map)
        if status_map.get((reg_code, dat)) == "done":
            continue

        try:
            n_cards = await fetch_with_retries(pool, reg_code, dat)
            done += 1
            total_cards += n_cards
            status_map[(reg_code, dat)] = "done"  # обновляем map

            # Прогресс каждые 10 задач или при значимых событиях
            if done % 10 == 0 or n_cards > 0:
                elapsed = time.monotonic() - start_time
                rate = (done - already_done) / elapsed if elapsed > 0 else 0
                remaining = total - done
                eta_sec = remaining / rate if rate > 0 else 0
                log.info(
                    f"[{done}/{total}] {reg_code} {reg_name} → {dat}: +{n_cards} карт. "
                    f"| скорость: {rate:.2f}/сек | ETA: {eta_sec/60:.0f} мин | всего: {total_cards}"
                )
        except Exception as e:
            failed.append((reg_code, reg_name, dat, str(e)))
            log.error(f"✗ FAILED {reg_code} {reg_name} → {dat}: {e}")
            continue  # не останавливаемся

    # Финальный отчёт
    elapsed = time.monotonic() - start_time
    log.info("=" * 70)
    log.info(f"✅ ETL завершён за {elapsed/60:.1f} мин")
    log.info(f"   Загружено задач: {done - already_done}")
    log.info(f"   Всего карточек: {total_cards}")
    log.info(f"   Ошибок: {len(failed)}")
    if failed:
        log.warning("Не удалось загрузить (можно перезапустить ETL — успешные будут пропущены):")
        for reg_code, reg_name, dat, err in failed[:20]:
            log.warning(f"  {reg_code} {reg_name} → {dat}: {err[:100]}")
        if len(failed) > 20:
            log.warning(f"  ... и ещё {len(failed) - 20}")

    # Проверим размеры таблиц
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            for tbl in ["gibdd_cards", "gibdd_vehicles", "gibdd_participants", "etl_log"]:
                await cur.execute(f"SELECT COUNT(*) AS c FROM {tbl}")
                cnt = (await cur.fetchone())["c"]
                log.info(f"  {tbl}: {cnt} записей")

            await cur.execute("SELECT pg_size_pretty(pg_database_size(current_database()))")
            size = (await cur.fetchone())["pg_size_pretty"]
            log.info(f"  Размер БД: {size}")

    await pool.close()
    if _shared_client and not _shared_client.is_closed:
        await _shared_client.aclose()
    log.info("✓ Пул соединений закрыт")


if __name__ == "__main__":
    asyncio.run(main())
