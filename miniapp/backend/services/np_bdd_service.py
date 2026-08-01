"""
np_bdd_service.py — сервисный слой НП БДД для Mini App.

Тонкая обёртка над модулем np_bdd/scripts/forecast.py.

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
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

# --- Пути к модулю np_bdd ------------------------------------------------
#
# Стратегия поиска NPBDD_ROOT (по приоритету):
#   1. Переменная окружения NPBDD_ROOT (явное указание)
#   2. Относительно этого файла: ../../../../np_bdd
#      (miniapp/backend/services/np_bdd_service.py → корень → np_bdd/)
#   3. Текущая рабочая директория + np_bdd/
#   4. /app/np_bdd/ (путь на Bothost при Docker-деплое)
#
# Все кандидаты логируются при первом обращении, чтобы в логах сервера
# было видно, какой путь выбран и почему.

_SERVICE_FILE = Path(__file__).resolve()
_CANDIDATE_ROOTS = [
    _SERVICE_FILE.parent.parent.parent.parent / "np_bdd",  # ../../../../np_bdd
    Path.cwd() / "np_bdd",
    Path("/app/np_bdd"),
    Path("/app/gibdd-bot/np_bdd"),
]

_env_root = os.environ.get("NPBDD_ROOT")
if _env_root:
    _CANDIDATE_ROOTS.insert(0, Path(_env_root))

NPBDD_ROOT: Path | None = None
for _cand in _CANDIDATE_ROOTS:
    if (_cand / "data" / "vehicles").is_dir():
        NPBDD_ROOT = _cand
        break

if NPBDD_ROOT is None:
    # fallback на дефолтный путь — даже если не существует, чтобы ошибки были осмысленные
    NPBDD_ROOT = _CANDIDATE_ROOTS[0]
    logger.error(
        "[np_bdd] НЕ НАЙДЕНА папка np_bdd/data/vehicles/ ни в одном из кандидатов: %s",
        [str(p) for p in _CANDIDATE_ROOTS],
    )

NPBDD_SCRIPTS = NPBDD_ROOT / "scripts"

# Логируем выбор пути (один раз при импорте)
logger.info("[np_bdd] NPBDD_ROOT resolved to: %s", NPBDD_ROOT)
logger.info("[np_bdd] NPBDD_SCRIPTS = %s", NPBDD_SCRIPTS)
logger.info("[np_bdd] CWD = %s", Path.cwd())
logger.info("[np_bdd] __file__ = %s", _SERVICE_FILE)
logger.info("[np_bdd] Candidates checked: %s", [str(p) for p in _CANDIDATE_ROOTS])

# Кэш: (region_code, plan_line_mode) → (payload, timestamp)
# TTL = 10 минут.
_CACHE: dict[tuple[str, str], tuple[dict[str, Any], float]] = {}
_CACHE_TTL_SEC = 600  # 10 минут


# --- Импорт forecast.py ---------------------------------------------------


_forecast_module = None


def _get_forecast():
    """Lazy-импорт forecast.py из np_bdd/scripts/."""
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


# --- Диагностика ----------------------------------------------------------


def get_debug_info() -> dict[str, Any]:
    """
    Возвращает диагностическую информацию о путях и наличии файлов.
    Используется эндпоинтом /api/np-bdd/_debug для отладки на сервере.
    """
    vehicles_dir = NPBDD_ROOT / "data" / "vehicles"
    plans_dir = NPBDD_ROOT / "data" / "plans"
    history_dir = NPBDD_ROOT / "data" / "history"
    freeze_dir = NPBDD_ROOT / "data" / "freeze"

    def _list_json(d: Path) -> list[str]:
        if not d.exists():
            return []
        return sorted(p.name for p in d.glob("*.json"))

    return {
        "service_file": str(_SERVICE_FILE),
        "cwd": str(Path.cwd()),
        "npbdd_root": str(NPBDD_ROOT),
        "npbdd_root_exists": NPBDD_ROOT.exists(),
        "npbdd_scripts": str(NPBDD_SCRIPTS),
        "candidates_checked": [str(p) for p in _CANDIDATE_ROOTS],
        "env_npbdd_root": _env_root,
        "data": {
            "vehicles_dir": str(vehicles_dir),
            "vehicles_exists": vehicles_dir.exists(),
            "vehicles_files": _list_json(vehicles_dir),
            "plans_dir": str(plans_dir),
            "plans_exists": plans_dir.exists(),
            "plans_files": _list_json(plans_dir),
            "history_dir": str(history_dir),
            "history_exists": history_dir.exists(),
            "history_files": _list_json(history_dir),
            "freeze_dir": str(freeze_dir),
            "freeze_exists": freeze_dir.exists(),
            "freeze_files": _list_json(freeze_dir),
            "seasonal_coefficients_exists": (NPBDD_ROOT / "data" / "seasonal_coefficients.json").exists(),
            "region_mapping_exists": (NPBDD_ROOT / "data" / "region_mapping.json").exists(),
            "user_settings_exists": (NPBDD_ROOT / "data" / "user_settings.json").exists(),
        },
        # Содержимое /app/ — верхний уровень, чтобы понять структуру
        "app_dir_listing": sorted(p.name for p in NPBDD_ROOT.parent.iterdir()) if NPBDD_ROOT.parent.exists() else [],
    }


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

    # Диагностика: если директории нет или пусты — логируем
    if not vehicles_dir.exists():
        logger.warning(
            "[np_bdd] Директория vehicles НЕ найдена: %s (NPBDD_ROOT=%s, CWD=%s)",
            vehicles_dir, NPBDD_ROOT, Path.cwd(),
        )
    if not plans_dir.exists():
        logger.warning(
            "[np_bdd] Директория plans НЕ найдена: %s (NPBDD_ROOT=%s, CWD=%s)",
            plans_dir, NPBDD_ROOT, Path.cwd(),
        )

    result: list[dict[str, Any]] = []
    if vehicles_dir.exists():
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
    """Сбрасывает кэш. Если region_code указан — только для этого региона."""
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
    """
    freeze_mod = _get_freeze_module()
    loop = asyncio.get_running_loop()

    def _do_freeze():
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
    """Возвращает список замороженных лет для региона."""
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
    invalidate_cache(region_code)
    return {
        "plan_line_mode": region_settings.get("plan_line_mode", "linear"),
    }
