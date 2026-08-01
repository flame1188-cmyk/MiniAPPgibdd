"""
np_bdd_service.py — сервисный слой НП БДД для Mini App.

Тонкая обёртка над модулем /home/z/my-project/npbdd/scripts/forecast.py.

Функции:
- list_regions(): список регионов из data/vehicles/ + data/plans/.
- get_data(region_code, plan_line_mode): runtime_calc_async + кэш 10 минут.
- freeze_year(region_code, year): ручная заморозка года (через freeze_year.py).
- unfreeze_year(region_code, year): разморозка.
- list_frozen_years(region_code): список замороженных лет.
- get_settings(region_code) / update_settings(region_code, ...): настройки плана.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import sys
import time
from pathlib import Path
from typing import Any, Literal

# --- Пути к модулю np_bdd (внутри gibdd-bot) -----------------------------
# miniapp/backend/services/np_bdd_service.py → ../.. = miniapp/
# → ../.. = gibdd-bot/ → / "np_bdd" = gibdd-bot/np_bdd/
NPBDD_ROOT = Path(__file__).resolve().parent.parent.parent.parent / "np_bdd"
NPBDD_SCRIPTS = NPBDD_ROOT / "scripts"

# Кэш: (region_code, plan_line_mode) → (payload, timestamp)
# TTL = 10 минут (для текущего года; история не меняется, но мы всё равно
# кэшируем весь payload — это удобно).
_CACHE: dict[tuple[str, str], tuple[dict[str, Any], float]] = {}
_CACHE_TTL_SEC = 600  # 10 минут


# --- Импорт forecast.py ---------------------------------------------------


_forecast_module = None


def _get_forecast():
    """Lazy-импорт forecast.py из npbdd/scripts/."""
    global _forecast_module
    if _forecast_module is not None:
        return _forecast_module
    if str(NPBDD_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(NPBDD_SCRIPTS))
    _forecast_module = importlib.import_module("forecast")
    return _forecast_module


def _get_freeze_module():
    if str(NPBDD_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(NPBDD_SCRIPTS))
    return importlib.import_module("freeze_year")


# --- Справочник регионов --------------------------------------------------


async def list_regions() -> list[dict[str, Any]]:
    """
    Возвращает список регионов, для которых есть vehicles + plans JSON.

    Структура элемента:
      {"code": "1106", "name": "г. Севастополь"}
    Сортировка по имени.
    """
    vehicles_dir = NPBDD_ROOT / "data" / "vehicles"
    plans_dir = NPBDD_ROOT / "data" / "plans"

    # Диагностика: если директории нет или пусты — логируем, чтобы было видно
    # в логах сервера (иначе glob() молча возвращает []).
    import sys
    if not vehicles_dir.exists():
        print(f"[np_bdd] ВНИМАНИЕ: директория не найдена: {vehicles_dir} "
              f"(NPBDD_ROOT={NPBDD_ROOT})", file=sys.stderr)
    if not plans_dir.exists():
        print(f"[np_bdd] ВНИМАНИЕ: директория не найдена: {plans_dir} "
              f"(NPBDD_ROOT={NPBDD_ROOT})", file=sys.stderr)

    result: list[dict[str, Any]] = []
    for veh_file in vehicles_dir.glob("*.json"):
        code = veh_file.stem
        try:
            veh = json.loads(veh_file.read_text(encoding="utf-8"))
            name = veh.get("region_name", code)
        except Exception:  # noqa: BLE001
            name = code
        # Проверяем, что есть и plans (иначе показывать бессмысленно).
        if not (plans_dir / f"{code}.json").exists():
            continue
        result.append({"code": code, "name": name})
    result.sort(key=lambda x: x["name"])
    return result


# --- Главный payload ------------------------------------------------------


async def get_data(
    region_code: str,
    plan_line_mode: Literal["linear", "horizontal"] = "linear",
    use_cache: bool = True,
) -> dict[str, Any]:
    """
    Возвращает runtime-расчёт для UI: история + текущий год + прогноз + KPI.

    Кэшируется на 10 минут по ключу (region_code, plan_line_mode).
    plan_line_mode влияет только на monthly_chart.plan_cumulative — поэтому
    можно кэшировать payload независимо и просто пересобирать план-серию
    при смене toggle. Но для простоты пока кэшируем весь payload.

    При ошибке (нет Ктс/плана/сетевая ошибка) — бросает RuntimeError.
    """
    cache_key = (region_code, plan_line_mode)
    if use_cache and cache_key in _CACHE:
        payload, ts = _CACHE[cache_key]
        if time.time() - ts < _CACHE_TTL_SEC:
            return payload

    forecast = _get_forecast()
    payload = await forecast.runtime_calc_async(region_code, plan_line_mode=plan_line_mode)

    if use_cache:
        _CACHE[cache_key] = (payload, time.time())
    return payload


def invalidate_cache(region_code: str | None = None) -> None:
    """
    Сбрасывает кэш. Если region_code указан — только для этого региона.
    Иначе — весь.
    """
    if region_code is None:
        _CACHE.clear()
    else:
        keys_to_del = [k for k in _CACHE if k[0] == region_code]
        for k in keys_to_del:
            del _CACHE[k]


# --- Заморозка года -------------------------------------------------------


async def freeze_year(region_code: str, year: int, note: str | None = None,
                      frozen_by: str = "miniapp") -> dict[str, Any]:
    """
    Замораживает год для региона. После заморозки год берётся из
    data/freeze/ и не пересчитывается.

    Возвращает структуру замороженной записи.
    """
    freeze_mod = _get_freeze_module()
    # freeze_year.py работает с файловой системой напрямую (синхронно).
    # Запускаем в executor, чтобы не блокировать event loop.
    loop = asyncio.get_running_loop()

    def _do_freeze():
        # Создаём временный argparse-like объект и вызываем cmd_freeze.
        # Проще: напрямую через load_freeze_file + get_year_data_for_freeze + save_freeze_file.
        payload = freeze_mod.load_freeze_file(region_code)
        snapshot = freeze_mod.get_year_data_for_freeze(region_code, year)
        record = {
            "deaths": snapshot["deaths"],
            "vehicles": snapshot["vehicles"],
            "tr": snapshot["tr"],
            "frozen_at": freeze_mod.date.today().isoformat(),
            "frozen_by": frozen_by,
        }
        if snapshot.get("source_deaths_breakdown"):
            record["source_deaths_breakdown"] = snapshot["source_deaths_breakdown"]
        if note:
            record["note"] = note
        payload["frozen_years"][str(year)] = record
        freeze_mod.save_freeze_file(payload)
        return record

    record = await loop.run_in_executor(None, _do_freeze)
    # После заморозки инвалидируем кэш по этому региону.
    invalidate_cache(region_code)
    return record


async def unfreeze_year(region_code: str, year: int) -> dict[str, Any]:
    """Размораживает год (если был заморожен). Возвращает {"ok": True/False}."""
    freeze_mod = _get_freeze_module()
    loop = asyncio.get_running_loop()

    def _do_unfreeze():
        payload = freeze_mod.load_freeze_file(region_code)
        year_str = str(year)
        if year_str not in payload["frozen_years"]:
            return False
        del payload["frozen_years"][year_str]
        freeze_mod.save_freeze_file(payload)
        return True

    ok = await loop.run_in_executor(None, _do_unfreeze)
    if ok:
        invalidate_cache(region_code)
    return {"ok": ok, "region_code": region_code, "year": year}


async def list_frozen_years(region_code: str) -> list[dict[str, Any]]:
    """
    Возвращает список замороженных лет для региона.
    Каждый элемент: {"year": 2025, "tr": 1.834, "deaths": 27, "frozen_at": "...", "note": "..."}
    """
    freeze_mod = _get_freeze_module()
    loop = asyncio.get_running_loop()

    def _do_list():
        payload = freeze_mod.load_freeze_file(region_code)
        result = []
        for year_str, rec in sorted(payload.get("frozen_years", {}).items()):
            result.append({
                "year": int(year_str),
                "tr": rec["tr"],
                "deaths": rec["deaths"],
                "vehicles": rec["vehicles"],
                "frozen_at": rec.get("frozen_at"),
                "frozen_by": rec.get("frozen_by"),
                "note": rec.get("note"),
            })
        return result

    return await loop.run_in_executor(None, _do_list)


# --- Настройки пользователя (для toggle linear/horizontal) ---------------


SETTINGS_FILE = NPBDD_ROOT / "data" / "user_settings.json"


def _load_all_settings() -> dict[str, Any]:
    if not SETTINGS_FILE.exists():
        return {}
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save_all_settings(data: dict[str, Any]) -> None:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                             encoding="utf-8")


async def get_settings(region_code: str) -> dict[str, Any]:
    """Возвращает настройки региона. По умолчанию plan_line_mode='linear'."""
    all_settings = _load_all_settings()
    region_settings = all_settings.get(region_code, {})
    return {
        "plan_line_mode": region_settings.get("plan_line_mode", "linear"),
    }


async def update_settings(region_code: str,
                          plan_line_mode: Literal["linear", "horizontal"] | None = None,
                          ) -> dict[str, Any]:
    """Обновляет настройки региона. None-поля не меняются."""
    all_settings = _load_all_settings()
    region_settings = all_settings.get(region_code, {})
    if plan_line_mode is not None:
        region_settings["plan_line_mode"] = plan_line_mode
    all_settings[region_code] = region_settings
    _save_all_settings(all_settings)
    # После смены plan_line_mode инвалидируем кэш — пересоберётся с новым режимом.
    invalidate_cache(region_code)
    return {
        "plan_line_mode": region_settings.get("plan_line_mode", "linear"),
    }
