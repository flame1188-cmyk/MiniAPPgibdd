"""
gibdd_adapter.py — мост между np_bdd-модулем и данными о ДТП.

ЗАДАЧА
======

Дать функцию fetch_deaths_by_month(region_code_excel, year), которая:
  1. Маппит Excel-код региона (ОКТМО-подобный) → ГИБДД-API-код.
  2. Сначала пытается загрузить данные из локальной PostgreSQL БД (таблица dtp_cards_archive).
  3. Если данных нет в БД, обращается к внешнему API ГИБДД через bot._fetch_cards_for_period.
  4. Агрегирует карточки в {month_str: deaths_int}.
  5. Возвращает результат + список ошибок.

КРИТИЧНО
========

- Функция АСИНХРОННАЯ.
- Приоритет источников: PostgreSQL БД > API ГИБДД.
- Кэш: bot._fetch_cards_for_period сам кэширует результат на 1 час.
- Карточка ДТП: card["pog"] — строка с числом погибших.
  card["date_dtp"] — строка "DD.MM.YYYY".

ИСПОЛЬЗОВАНИЕ
=============

    # Из асинхронного контекста (бот):
    from gibdd_adapter import fetch_deaths_by_month
    deaths_by_month, errors = await fetch_deaths_by_month("1106", 2026)

    # Из синхронного кода (CLI):
    import asyncio
    deaths_by_month, errors = asyncio.run(fetch_deaths_by_month("1106", 2026))
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

# --- Относительные пути ---------------------------------------------------
# scripts/ → np_bdd/ → gibdd-bot/
NPBDD_ROOT = Path(__file__).resolve().parent.parent
GIBDD_BOT_ROOT = NPBDD_ROOT.parent  # = gibdd-bot/
REGION_MAPPING_FILE = NPBDD_ROOT / "datasets" / "region_mapping.json"


# --- Маппинг кодов регионов ----------------------------------------------


def load_region_mapping() -> dict[str, dict[str, str]]:
    """Загружает таблицу соответствия Excel-кодов → ГИБДД-кодов."""
    if not REGION_MAPPING_FILE.exists():
        return {}
    with REGION_MAPPING_FILE.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    return data.get("mappings", {})


def resolve_gibdd_code(excel_code: str) -> str:
    """
    Возвращает ГИБДД-API-код по Excel-коду региона.

    Если код найден в region_mapping.json — берётся оттуда.
    Иначе предполагается, что коды совпадают (возврат excel_code как есть).
    """
    mappings = load_region_mapping()
    entry = mappings.get(excel_code)
    if entry and "gibdd_code" in entry:
        return entry["gibdd_code"]
    return excel_code


# --- Загрузка модулей -----------------------------------------------------

_bot_module = None
_web_fallback_module = None
_db_adapter_module = None
_use_direct_web_fallback = False


def _get_bot_module():
    """
    Импортирует модуль bot из gibdd-bot. Кэширует результат.

    gibdd-bot не является пакетом — добавляем его корень в sys.path.
    Возвращает (bot_module, fetch_func) — где fetch_func это либо
    bot._fetch_cards_for_period (если бот загрузился), либо
    web_fallback.fetch_dtp_via_web_period (фолбэк для CLI/тестов без
    telegram-зависимостей).
    """
    global _bot_module, _web_fallback_module, _use_direct_web_fallback
    bot_root_str = str(GIBDD_BOT_ROOT)
    if bot_root_str not in sys.path:
        sys.path.insert(0, bot_root_str)

    # Путь 1: импортируем bot (даёт кэш + API).
    if _bot_module is None and not _use_direct_web_fallback:
        try:
            _bot_module = importlib.import_module("bot")
        except Exception as exc:  # noqa: BLE001
            print(f"[gibdd_adapter] ВНИМАНИЕ: не удалось импортировать bot "
                  f"({type(exc).__name__}: {exc}). "
                  f"Переключаюсь на прямой импорт web_fallback (без LRU-кэша).",
                  file=sys.stderr)
            _use_direct_web_fallback = True

    # Путь 2: импортируем web_fallback напрямую.
    if _use_direct_web_fallback and _web_fallback_module is None:
        _web_fallback_module = importlib.import_module("web_fallback")

    if _bot_module is not None:
        return _bot_module, _bot_module._fetch_cards_for_period
    return _web_fallback_module, _web_fallback_module.fetch_dtp_via_web_period


def _get_db_adapter():
    """
    Импортирует db_adapter для загрузки данных из PostgreSQL.
    Возвращает модуль или None, если импорт не удался.
    """
    global _db_adapter_module
    if _db_adapter_module is None:
        try:
            _db_adapter_module = importlib.import_module("np_bdd.scripts.db_adapter")
        except Exception as exc:  # noqa: BLE001
            print(f"[gibdd_adapter] ВНИМАНИЕ: не удалось импортировать db_adapter "
                  f"({type(exc).__name__}: {exc}). Данные будут грузиться только из API.",
                  file=sys.stderr)
            return None
    return _db_adapter_module


# --- Агрегация карточек ---------------------------------------------------


def aggregate_cards_to_monthly_deaths(cards: list[dict[str, Any]]) -> dict[str, int]:
    """
    Агрегирует список карточек ДТП в {month_str: deaths_int}.

    card["date_dtp"] — строка "DD.MM.YYYY"
    card["pog"] — строка с числом погибших ("0", "1", "2", ...)

    Возвращает словарь вида {"1": 2, "2": 1, ...} для всех месяцев,
    в которых был хотя бы один месяц (включая нулевые месяцы в середине).
    Месяцы без ДТП будут отсутствовать в результате — caller должен
    добить нулями через .get(month, 0).
    """
    deaths_by_month: dict[str, int] = {}
    for card in cards:
        date_str = card.get("date_dtp", "")
        if not date_str or "." not in date_str:
            continue
        parts = date_str.split(".")
        if len(parts) < 2:
            continue
        try:
            month = int(parts[1])
        except ValueError:
            continue
        if month < 1 or month > 12:
            continue
        try:
            deaths = int(card.get("pog") or 0)
        except (ValueError, TypeError):
            deaths = 0
        m_str = str(month)
        deaths_by_month[m_str] = deaths_by_month.get(m_str, 0) + deaths
    return deaths_by_month


# --- Главная функция ------------------------------------------------------


async def fetch_deaths_by_month(
    region_code_excel: str,
    year: int,
    current_month: int | None = None,
) -> tuple[dict[str, int], list[str]]:
    """
    Получает {месяц: погибших} для региона за указанный год (с января по current_month).

    Приоритет источников данных:
      1. Локальная PostgreSQL БД (таблица dtp_cards_archive) — быстро, без нагрузки на API.
      2. Внешнее API ГИБДД (через bot._fetch_cards_for_period или web_fallback) — если в БД нет данных.

    Args:
        region_code_excel: код региона в формате Excel пользователя (напр. "1106").
        year: год (напр. 2026).
        current_month: последний месяц для загрузки (1..12).
            Если None — берётся текущий календарный месяц.

    Returns:
        (deaths_by_month, errors)
        deaths_by_month: {"1": 2, "2": 1, "3": 3, ...}
        errors: список строковых описаний ошибок (пустой, если всё OK).
    """
    if current_month is None:
        current_month = date.today().month
    if current_month < 1 or current_month > 12:
        raise ValueError(f"current_month must be 1..12, got {current_month}")

    # 1. Маппинг кода региона
    gibdd_code = resolve_gibdd_code(region_code_excel)

    # 2. Формируем dat_list: ["1.2026", "2.2026", ..., "N.2026"]
    dat_list = [f"{m}.{year}" for m in range(1, current_month + 1)]

    # 3. Пытаемся загрузить данные из БД (приоритетный источник)
    db_adapter = _get_db_adapter()
    all_cards = []
    errors = []
    months_from_db = set()
    
    if db_adapter is not None:
        # Загружаем данные по месяцам из БД
        for month_num in range(1, current_month + 1):
            try:
                cards = await db_adapter.fetch_cards_from_db(gibdd_code, month_num, year)
                if cards:
                    all_cards.extend(cards)
                    months_from_db.add(month_num)
            except Exception as e:  # noqa: BLE001
                errors.append(f"Ошибка при загрузке из БД за {month_num}.{year}: {e}")
    
    # 4. Для месяцев, которых нет в БД, загружаем через API
    months_missing = [m for m in range(1, current_month + 1) if m not in months_from_db]
    
    if months_missing:
        # Загружаем модуль и выбираем функцию для запроса карточек.
        _, fetch_func = _get_bot_module()
        
        # Формируем dat_list только для недостающих месяцев
        missing_dat_list = [f"{m}.{year}" for m in months_missing]
        
        try:
            # Сигнатуры немного различаются:
            # - bot._fetch_cards_for_period(dat_list, reg_code, log_prefix, ..., cache_result=True)
            # - web_fallback.fetch_dtp_via_web_period(dat_list, reg_api, log_prefix, progress_callback)
            import inspect
            sig = inspect.signature(fetch_func)
            if "cache_result" in sig.parameters:
                # Это bot._fetch_cards_for_period
                cards, api_errors = await fetch_func(
                    dat_list=missing_dat_list,
                    reg_code=gibdd_code,
                    log_prefix=f"NPBDD[{region_code_excel}→{gibdd_code}] (API)",
                    cache_result=True,
                )
            else:
                # Это web_fallback.fetch_dtp_via_web_period
                cards, api_errors = await fetch_func(
                    missing_dat_list,
                    gibdd_code,
                    log_prefix=f"NPBDD[{region_code_excel}→{gibdd_code}] (API)",
                )
            
            all_cards.extend(cards)
            errors.extend(api_errors)
            
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Ошибка при вызове API: {exc}")

    # 5. Агрегируем карточки
    deaths_by_month = aggregate_cards_to_monthly_deaths(all_cards)
    return deaths_by_month, errors


# --- Синхронная обёртка для CLI -------------------------------------------


def fetch_deaths_by_month_sync(
    region_code_excel: str,
    year: int,
    current_month: int | None = None,
) -> tuple[dict[str, int], list[str]]:
    """
    Синхронная обёртка над fetch_deaths_by_month для использования в CLI.

    ВНИМАНИЕ: нельзя вызывать из асинхронного контекста (бот).
    Для бота используйте `await fetch_deaths_by_month(...)`.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None:
        raise RuntimeError(
            "fetch_deaths_by_month_sync нельзя вызывать из event loop. "
            "Используйте `await fetch_deaths_by_month(...)`."
        )

    return asyncio.run(fetch_deaths_by_month(region_code_excel, year, current_month))


# --- CLI ------------------------------------------------------------------


def main(argv: list[str]) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Адаптер НП БДД → gibdd-bot")
    parser.add_argument("--region", required=True, help="Excel-код региона (напр. 1106)")
    parser.add_argument("--year", type=int, default=date.today().year)
    parser.add_argument("--month", type=int, default=None,
                        help="Последний месяц для загрузки (по умолчанию текущий)")
    args = parser.parse_args(argv[1:])

    print(f"[gibdd_adapter] Запрос: регион {args.region}, год {args.year}, "
          f"месяц {args.month or 'текущий'}")

    gibdd_code = resolve_gibdd_code(args.region)
    print(f"[gibdd_adapter] Маппинг: {args.region} → {gibdd_code}")

    deaths_by_month, errors = fetch_deaths_by_month_sync(
        args.region, args.year, args.month,
    )

    print(f"[gibdd_adapter] Ошибки ({len(errors)}):")
    for e in errors[:5]:
        print(f"  - {e}")

    print(f"[gibdd_adapter] deaths_by_month: {deaths_by_month}")
    print(f"[gibdd_adapter] Итого погибших: {sum(deaths_by_month.values())}")
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
