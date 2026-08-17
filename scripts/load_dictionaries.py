#!/usr/bin/env python3
"""
Загружает актуальные справочники регионов и показателей в таблицы
gibdd_regions и gibdd_indicators.

Источники (по приоритету):
  1. API ГИБДД  (http://стат.гибдд.рф/opendataapi/v1/dictionary/rows?code=1)
  2. Встроенный эталон regions_builtin.py / regions_builtin.json
     (90 регионов РФ, обновлён 2026-08-16)

Если API ГИБДД недоступен (таймаут, 5xx, нет сети) — автоматически
используется встроенный эталон. Это гарантирует, что gibdd_regions
всегда заполнена перед запуском etl_archive.py.

ОБЯЗАТЕЛЬНО запустить ОДИН РАЗ перед первым запуском etl_archive.py —
иначе ETL не будет знать, какие регионы обрабатывать.

Запуск:
  python3 load_dictionaries.py
  python3 load_dictionaries.py --env-file /path/to/.env
  python3 load_dictionaries.py --no-api           # только из эталона
  python3 load_dictionaries.py --verify-only      # только проверить счётчик
  DATABASE_URL=postgres://... python3 load_dictionaries.py
"""
import argparse
import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

# Windows: psycopg3 async требует SelectorEventLoop (а не Proactor по умолчанию).
# Это нужно сделать ДО первого asyncio.run().
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent  # корень репозитория MiniAPPgibdd

# ─────────────────────────────────────────────────────────────────────────
# Загрузка .env (та же логика, что в etl_archive.py)
# ─────────────────────────────────────────────────────────────────────────
# Pre-parse --env-file из sys.argv, потому что argparse main() парсит позже
_env_file_cli = None
for _i, _a in enumerate(sys.argv):
    if _a == "--env-file" and _i + 1 < len(sys.argv):
        _env_file_cli = sys.argv[_i + 1]
        break
    if _a.startswith("--env-file="):
        _env_file_cli = _a.split("=", 1)[1]
        break

_env_candidates = []
if _env_file_cli:
    _env_candidates.append(Path(_env_file_cli))
if os.environ.get("ETL_ENV_FILE"):
    _env_candidates.append(Path(os.environ["ETL_ENV_FILE"]))
_env_candidates.append(SCRIPT_DIR / ".env")
_env_candidates.append(REPO_ROOT / ".env")
_env_candidates.append(REPO_ROOT / "gibdd-bot" / ".env")

_env_path_used = None
for _cand in _env_candidates:
    if _cand.is_file():
        for _line in _cand.read_text(encoding="utf-8").splitlines():
            _line = _line.strip().rstrip("\r")  # отбрасываем CRLF (Windows)
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                _k = _k.strip()
                os.environ[_k] = _v.strip()
        _env_path_used = _cand
        break

try:
    import httpx
    import psycopg
except ImportError as _imp_err:
    print(f"ERROR: missing dependency: {_imp_err}", file=sys.stderr)
    print("  pip install httpx 'psycopg[binary]'", file=sys.stderr)
    sys.exit(2)

DB_URL = os.environ.get("DATABASE_URL")
if not DB_URL:
    print(
        "FATAL: DATABASE_URL is not set. Use --env-file, ETL_ENV_FILE, or export DATABASE_URL",
        file=sys.stderr,
    )
    sys.exit(2)

GIBDD_BASE = "http://xn--80a7adb.xn--90adear.xn--p1ai"
GIBDD_TIMEOUT = 30.0  # секунд на справочник
EXPECTED_REGIONS_COUNT = 90  # РФ без 1100 (РФ целиком)


# ─────────────────────────────────────────────────────────────────────────
# Эталон (fallback, когда API недоступен)
# ─────────────────────────────────────────────────────────────────────────
def load_builtin_regions() -> list[dict]:
    """
    Возвращает список [{'code': '1101', 'name': 'Алтайский край'}, ...]
    из regions_builtin.py / regions_builtin.json.

    Пытаемся .py сначала (быстрее, без JSON-парсинга), потом .json.
    """
    # 1. .py — это список словарей {code, name}
    py_path = REPO_ROOT / "regions_builtin.py"
    if py_path.is_file():
        try:
            sys.path.insert(0, str(REPO_ROOT))
            import regions_builtin  # type: ignore
            return list(regions_builtin.BUILTIN_REGIONS)
        except Exception as exc:
            print(f"  WARN: regions_builtin.py import failed: {exc}", file=sys.stderr)

    # 2. .json
    json_path = REPO_ROOT / "regions_builtin.json"
    if json_path.is_file():
        import json
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            # Нормализуем ключи: могут быть {'code','name'} или {'rows_code','rows_name'}
            result = []
            for r in data:
                code = r.get("code") or r.get("rows_code")
                name = r.get("name") or r.get("rows_name")
                if code and name:
                    result.append({"code": str(code), "name": str(name)})
            return result
        except Exception as exc:
            print(f"  WARN: regions_builtin.json parse failed: {exc}", file=sys.stderr)

    return []


def load_builtin_indicators() -> list[dict]:
    """
    Возвращает [{'code': '1', 'name': 'Все ДТП'}, ...] — упрощённый список
    показателей аварийности для fallback'а, когда API недоступен.
    Эти коды покрывают все типы запросов, которые делает бот/MiniApp.
    """
    return [
        {"code": "1", "name": "Все ДТП"},
        {"code": "2", "name": "ДТП со смертельным исходом"},
        {"code": "3", "name": "ДТП с пострадавшими"},
        {"code": "4", "name": "ДТП с материальным ущербом"},
        {"code": "5", "name": "ДТП по вине нетрезвых водителей"},
        {"code": "6", "name": "ДТП по вине водителей ТС с иностранными регистрационными знаками"},
        {"code": "7", "name": "ДТП по вине пешеходов"},
        {"code": "8", "name": "ДТП с участием детей"},
        {"code": "9", "name": "ДТП с участием пешеходов"},
        {"code": "10", "name": "ДТП с участием велосипедистов"},
        {"code": "11", "name": "ДТП с участием мотоциклистов и мопедистов"},
        {"code": "12", "name": "ДТП с участием ТС, эксплуатация которых запрещена"},
        {"code": "13", "name": "ДТП с тяжкими последствиями"},
        {"code": "14", "name": "ДТП на железнодорожных переездах"},
        {"code": "15", "name": "ДТП на остановках общественного транспорта"},
        {"code": "16", "name": "ДТП в местах съезда/выезда с прилегающей территории"},
    ]


# ─────────────────────────────────────────────────────────────────────────
# API ГИБДД
# ─────────────────────────────────────────────────────────────────────────
def parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%d.%m.%Y").date()
    except Exception:
        return None


async def fetch_dictionary(client: httpx.AsyncClient, code: int) -> list[dict]:
    """Загружает справочник по коду (1=регионы, 2=показатели)."""
    url = f"{GIBDD_BASE}/opendataapi/v1/dictionary/rows"
    resp = await client.get(url, params={"code": code}, timeout=GIBDD_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    return data["results"][0]["dict_rows"]


async def fetch_regions_from_api() -> tuple[list[dict], list[dict]] | None:
    """
    Возвращает (regions, indicators) из API ГИБДД, или None при ошибке сети.

    Каждый элемент regions: {'rows_code': '1101', 'rows_name': '...', 'start_date', 'finish_date'}
    Каждый элемент indicators: аналогично для pok.
    """
    try:
        async with httpx.AsyncClient() as client:
            regions = await fetch_dictionary(client, 1)
            print(f"  Регионов получено из API: {len(regions)}")
            indicators = await fetch_dictionary(client, 2)
            print(f"  Показателей получено из API: {len(indicators)}")
        return regions, indicators
    except Exception as exc:
        print(f"  WARN: API ГИБДД недоступен: {type(exc).__name__}: {exc}")
        print(f"  → Переключаемся на встроенный эталон (regions_builtin)")
        return None


# ─────────────────────────────────────────────────────────────────────────
# DB: INSERT/UPSERT
# ─────────────────────────────────────────────────────────────────────────
async def upsert_regions(cur, rows: list[tuple[str, str, object, object]]) -> int:
    """UPSERT списка регионов. rows = [(code, name, start_date, finish_date), ...]"""
    inserted = 0
    for code, name, start_date, finish_date in rows:
        if code == "1100":  # РФ целиком — не регион
            continue
        await cur.execute(
            """
            INSERT INTO gibdd_regions (reg_code, reg_name, start_date, finish_date, is_active, updated_at)
            VALUES (%s, %s, %s, %s, TRUE, NOW())
            ON CONFLICT (reg_code) DO UPDATE SET
                reg_name = EXCLUDED.reg_name,
                start_date = EXCLUDED.start_date,
                finish_date = EXCLUDED.finish_date,
                is_active = TRUE,
                updated_at = NOW()
            """,
            (code, name, start_date, finish_date),
        )
        inserted += 1
    return inserted


async def upsert_indicators(cur, rows: list[tuple[str, str, object, object]]) -> int:
    """UPSERT списка показателей."""
    inserted = 0
    for code, name, start_date, finish_date in rows:
        await cur.execute(
            """
            INSERT INTO gibdd_indicators (pok_code, pok_name, start_date, finish_date, updated_at)
            VALUES (%s, %s, %s, %s, NOW())
            ON CONFLICT (pok_code) DO UPDATE SET
                pok_name = EXCLUDED.pok_name,
                start_date = EXCLUDED.start_date,
                finish_date = EXCLUDED.finish_date,
                updated_at = NOW()
            """,
            (code, name, start_date, finish_date),
        )
        inserted += 1
    return inserted


# ─────────────────────────────────────────────────────────────────────────
# Верификация
# ─────────────────────────────────────────────────────────────────────────
async def verify_counts(conn) -> tuple[int, int]:
    """Возвращает (regions_count, indicators_count)."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT COUNT(*) FROM gibdd_regions")
        regions_count = (await cur.fetchone())[0]
        await cur.execute("SELECT COUNT(*) FROM gibdd_indicators")
        indicators_count = (await cur.fetchone())[0]
    return regions_count, indicators_count


# ─────────────────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────────────────
async def main():
    parser = argparse.ArgumentParser(description="Загрузка справочников ГИБДД")
    parser.add_argument("--env-file", default=None, help="Путь к .env")
    parser.add_argument(
        "--no-api",
        action="store_true",
        help="Не ходить в API ГИБДД — использовать только встроенный эталон",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Только проверить счётчики в gibdd_regions/gibdd_indicators, ничего не меняя",
    )
    args = parser.parse_args()

    print("=== Загрузка справочников в gibdd_regions / gibdd_indicators ===")
    print(f"DB URL: {DB_URL[:30]}...{DB_URL[-15:]}")
    if _env_path_used:
        print(f".env loaded from: {_env_path_used}")
    print()

    # --verify-only
    if args.verify_only:
        async with await psycopg.AsyncConnection.connect(DB_URL, autocommit=True) as conn:
            r_cnt, i_cnt = await verify_counts(conn)
            print(f"gibdd_regions:    {r_cnt} строк")
            print(f"gibdd_indicators: {i_cnt} строк")
            ok = (r_cnt == EXPECTED_REGIONS_COUNT)
            print(f"\nОжидаемое число регионов: {EXPECTED_REGIONS_COUNT}")
            print(f"  → {'OK' if ok else 'MISMATCH!'}")
            sys.exit(0 if ok else 1)

    # 1. Получаем справочники
    api_data = None
    if not args.no_api:
        api_data = await fetch_regions_from_api()

    if api_data is not None:
        # API-путь: сохраняем start_date / finish_date из ответа
        regions_raw, indicators_raw = api_data
        regions_rows = [
            (str(r["rows_code"]), r["rows_name"],
             parse_date(r.get("start_date")), parse_date(r.get("finish_date")))
            for r in regions_raw
        ]
        indicators_rows = [
            (str(ind["rows_code"]), ind["rows_name"],
             parse_date(ind.get("start_date")), parse_date(ind.get("finish_date")))
            for ind in indicators_raw
        ]
        source = "API ГИБДД"
    else:
        # Fallback-путь: встроенный эталон
        builtin_regs = load_builtin_regions()
        if not builtin_regs:
            print("FATAL: встроенный эталон недоступен (regions_builtin.py/.json не найдены)")
            sys.exit(2)
        if len(builtin_regs) != EXPECTED_REGIONS_COUNT:
            print(f"WARN: эталон содержит {len(builtin_regs)} регионов, "
                  f"ожидается {EXPECTED_REGIONS_COUNT}")
        regions_rows = [(r["code"], r["name"], None, None) for r in builtin_regs]
        indicators_rows = [(ind["code"], ind["name"], None, None)
                            for ind in load_builtin_indicators()]
        source = "встроенный эталон (regions_builtin)"

    print(f"Источник: {source}")
    print(f"Регионов к загрузке: {len(regions_rows)} (после фильтрации 1100)")
    print(f"Показателей к загрузке: {len(indicators_rows)}")
    print()

    # 2. UPSERT в БД
    async with await psycopg.AsyncConnection.connect(DB_URL, autocommit=True) as conn:
        async with conn.cursor() as cur:
            # Не делаем TRUNCATE — UPSERT безопаснее (не теряем FK на gibdd_regions
            # из etl_log, если будут добавлены).
            inserted_r = await upsert_regions(cur, regions_rows)
            print(f"✓ Регионов UPSERT'ено: {inserted_r}")

            inserted_i = await upsert_indicators(cur, indicators_rows)
            print(f"✓ Показателей UPSERT'ено: {inserted_i}")

        # 3. Верификация
        r_cnt, i_cnt = await verify_counts(conn)
        print()
        print(f"Итого в gibdd_regions:    {r_cnt}")
        print(f"Итого в gibdd_indicators: {i_cnt}")

        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT reg_code, reg_name FROM gibdd_regions ORDER BY reg_code LIMIT 5"
            )
            print("\nПервые 5 регионов:")
            async for row in cur:
                print(f"  {row[0]}  {row[1]}")

            await cur.execute(
                "SELECT pok_code, pok_name FROM gibdd_indicators "
                "WHERE pok_code = '1'"
            )
            row = await cur.fetchone()
            if row:
                print(f"\nPok=1: {row[1]}")

    ok = (r_cnt == EXPECTED_REGIONS_COUNT)
    print()
    if ok:
        print(f"✓ Успешно: gibdd_regions содержит {r_cnt} регионов (ожидается {EXPECTED_REGIONS_COUNT})")
        sys.exit(0)
    else:
        print(f"✗ ОШИБКА: gibdd_regions содержит {r_cnt}, ожидалось {EXPECTED_REGIONS_COUNT}")
        sys.exit(1)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
