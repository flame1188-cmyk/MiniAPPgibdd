"""
forecast.py — сезонная корректировка и расчёт runtime-метрик текущего года.

ЗАДАЧИ
======

1. Загрузить сезонные коэффициенты из data/seasonal_coefficients.json.
   Если файла нет — создать с дефолтными значениями (равномерное распределение
   1/12 на каждый месяц) и предупредить пользователя.

2. Дать две функции:
   - forecast_full_year_deaths(deaths_ytd, current_month) → int
       Прогноз погибших на конец года по сезонной корректировке.
   - build_monthly_cumulative_tr(deaths_by_month_actual,
                                  deaths_forecast_full_year,
                                  vehicles_year, plan_tr_year,
                                  plan_line_mode) → dict
       Формирует данные для графика 2 (кумулятивный Тр по месяцам):
       - фактическая часть (сплошная линия)
       - прогнозная часть (пунктир)
       - линия плана (линейный рост ИЛИ горизонтальная — по toggle)

3. Дать функцию runtime_calc(region_code) → dict, которая собирает всё
   вместе для отдачи в UI: история + текущий год + прогноз + план + KPI.

ИСПОЛЬЗОВАНИЕ
=============

    from forecast import runtime_calc
    payload = runtime_calc("67")
    # payload готов к сериализации и отправке в UI бота

АДМИНИСТРАТИВНЫЙ ИНТЕРФЕЙС
==========================

    python np_bdd/scripts/forecast.py --recalc-seasonal
        Пересчитать seasonal_coefficients.json по имеющейся истории.

    python np_bdd/scripts/forecast.py --region 67
        Напечатать runtime_calc для региона 67 (для отладки).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal

# --- Относительные пути (модуль самодостаточен, не зависит от хардкода) ----
# scripts/ → родитель = np_bdd/
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_HIST_DIR = PROJECT_ROOT / "data" / "history"
DATA_PLANS_DIR = PROJECT_ROOT / "data" / "plans"
DATA_VEHI_DIR = PROJECT_ROOT / "data" / "vehicles"
DATA_FREEZE_DIR = PROJECT_ROOT / "data" / "freeze"
SEASONAL_FILE = PROJECT_ROOT / "data" / "seasonal_coefficients.json"

TODAY = date.today()


# --- Сезонные коэффициенты -------------------------------------------------


DEFAULT_MONTHLY_SHARE = {str(m): round(1 / 12, 4) for m in range(1, 13)}


def load_seasonal_coefficients() -> dict[str, Any]:
    """Загружает seasonal_coefficients.json или создаёт дефолтный."""
    if not SEASONAL_FILE.exists():
        print(f"[forecast] ВНИМАНИЕ: {SEASONAL_FILE} не найден. "
              f"Создан дефолтный (равномерное распределение 1/12). "
              f"Запустите --recalc-seasonal после появления истории.")
        cumulative = {}
        running = 0.0
        for m in range(1, 13):
            running += DEFAULT_MONTHLY_SHARE[str(m)]
            cumulative[str(m)] = round(running, 4)
        payload = {
            "updated_at": TODAY.isoformat(),
            "method": "default uniform 1/12 (no history yet)",
            "monthly_share": DEFAULT_MONTHLY_SHARE,
            "cumulative_share": cumulative,
        }
        SEASONAL_FILE.parent.mkdir(parents=True, exist_ok=True)
        with SEASONAL_FILE.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        return payload
    with SEASONAL_FILE.open("r", encoding="utf-8") as fh:
        return json.load(fh)


# --- Прогноз ---------------------------------------------------------------


def forecast_full_year_deaths(deaths_ytd: int, current_month: int,
                              seasonal: dict[str, Any] | None = None) -> int:
    """
    Прогноз погибших на конец года по сезонной корректировке.

    Формула: deaths_ytd / cumulative_share[current_month]

    Если current_month == 12 — возвращаем deaths_ytd без изменений
    (год закончился, прогноз = факт).

    Если deaths_ytd == 0 — возвращаем 0 (предотвращаем деление 0/0).
    """
    if current_month < 1 or current_month > 12:
        raise ValueError(f"current_month must be 1..12, got {current_month}")
    if current_month == 12 or deaths_ytd == 0:
        return int(deaths_ytd)
    if seasonal is None:
        seasonal = load_seasonal_coefficients()
    cum_share = float(seasonal["cumulative_share"][str(current_month)])
    if cum_share <= 0:
        return int(deaths_ytd)
    return int(round(deaths_ytd / cum_share))


# --- Кумулятивный Тр по месяцам (для графика 2) ----------------------------


def build_monthly_cumulative_tr(
    deaths_by_month_actual: dict[str, int],
    deaths_forecast_full_year: int,
    vehicles_year: int,
    plan_tr_year: float,
    plan_line_mode: Literal["linear", "horizontal"] = "linear",
    current_month: int | None = None,
) -> dict[str, Any]:
    """
    Формирует структуру для графика 2.

    Возвращает:
    {
      "months": [1..12],
      "tr_actual_cumulative": {"1": 0.31, "2": 0.62, ...},   # сплошная
      "tr_forecast_cumulative": {"7": ..., "8": ..., ...},    # пунктир
      "plan_cumulative": {"1": plan/12, "2": 2*plan/12, ...}  # линейный
                    OR {"1": plan, "2": plan, ...}            # горизонтальный
      "current_month": 6,
      "plan_line_mode": "linear"
    }

    Если current_month не задан — берётся текущий календарный месяц.
    """
    if current_month is None:
        current_month = TODAY.month
    if vehicles_year <= 0:
        raise ValueError("vehicles_year must be > 0")

    # Доля прогноза на оставшиеся месяцы.
    seasonal = load_seasonal_coefficients()
    monthly_share = seasonal["monthly_share"]

    # Фактические месяцы: считаем кумулятивный Тр нарастающим итогом.
    tr_actual_cum: dict[str, float] = {}
    deaths_cum = 0
    for m in range(1, current_month + 1):
        deaths_cum += int(deaths_by_month_actual.get(str(m), 0))
        tr_actual_cum[str(m)] = round((deaths_cum * 10000) / vehicles_year, 3)

    # Прогнозные месяцы: если forecast_full_year задан, распределить
    # остаток по seasonal_share оставшихся месяцев.
    tr_forecast_cum: dict[str, float] = {}
    if current_month < 12 and deaths_forecast_full_year > 0:
        # Сколько погибших "уже в факте".
        deaths_actual_total = sum(
            int(deaths_by_month_actual.get(str(m), 0))
            for m in range(1, current_month + 1)
        )
        deaths_remaining = max(0, deaths_forecast_full_year - deaths_actual_total)

        # Доля оставшихся месяцев в общем годовом распределении.
        remaining_share_total = sum(
            float(monthly_share[str(m)])
            for m in range(current_month + 1, 13)
        )
        if remaining_share_total <= 0:
            # Непредвиденная ситуация (все доли в прошедших месяцах).
            # Фолбэк: равномерно по оставшимся месяцам.
            per_month_remaining = deaths_remaining / max(1, (12 - current_month))
            remaining_breakdown = {
                str(m): per_month_remaining
                for m in range(current_month + 1, 13)
            }
        else:
            remaining_breakdown = {
                str(m): deaths_remaining * float(monthly_share[str(m)]) / remaining_share_total
                for m in range(current_month + 1, 13)
            }

        # Кумулятивно.
        running_deaths = deaths_actual_total
        for m in range(current_month + 1, 13):
            running_deaths += remaining_breakdown[str(m)]
            tr_forecast_cum[str(m)] = round((running_deaths * 10000) / vehicles_year, 3)

    # План: линейный рост или горизонтальная линия.
    plan_cum: dict[str, float] = {}
    for m in range(1, 13):
        if plan_line_mode == "linear":
            plan_cum[str(m)] = round(plan_tr_year * m / 12, 3)
        else:  # horizontal
            plan_cum[str(m)] = round(plan_tr_year, 3)

    return {
        "months": list(range(1, 13)),
        "tr_actual_cumulative": tr_actual_cum,
        "tr_forecast_cumulative": tr_forecast_cum,
        "plan_cumulative": plan_cum,
        "current_month": current_month,
        "plan_line_mode": plan_line_mode,
    }


# --- Runtime-сборка для региона -------------------------------------------


def load_json_if_exists(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def get_year_data(region_code: str, year: int) -> dict[str, Any] | None:
    """
    Возвращает данные за указанный год с приоритетом:
    freeze > history > None (текущий год считается отдельно).

    Для замороженного года — структура с пометкой frozen=True.
    Для исторического — из history.
    """
    freeze = load_json_if_exists(DATA_FREEZE_DIR / f"{region_code}.json")
    if freeze and str(year) in freeze.get("frozen_years", {}):
        rec = freeze["frozen_years"][str(year)]
        return {
            "deaths": rec["deaths"],
            "vehicles": rec["vehicles"],
            "tr": rec["tr"],
            "frozen": True,
            "frozen_at": rec.get("frozen_at"),
            "source": "freeze",
        }

    history = load_json_if_exists(DATA_HIST_DIR / f"{region_code}.json")
    if history and str(year) in history.get("years", {}):
        rec = history["years"][str(year)]
        return {
            "deaths": rec["deaths"],
            "vehicles": rec["vehicles"],
            "tr": rec["tr"],
            "frozen": False,
            "source": "history",
        }
    return None


def fetch_actual_deaths_from_web(region_code: str, year: int) -> dict[str, int]:
    """
    Получает фактические погибшие по месяцам из карточек ДТП через gibdd-bot.

    Делегирует работу в gibdd_adapter.fetch_deaths_by_month_sync, который:
    1. Маппит Excel-код региона → ГИБДД-API-код (напр. 1106 → 1167 для Севастополя).
    2. Загружает карточки через bot._fetch_cards_for_period (API + web_fallback + кэш).
    3. Агрегирует карточки в {месяц: погибших}.

    ВНИМАНИЕ: функция синхронная, использует asyncio.run() — НЕ вызывать
    из асинхронного контекста (бота). Для бота используйте async-версию
    fetch_actual_deaths_from_web_async().

    При ошибке возвращает пустой словарь (логирует в stderr).
    """
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).parent))
    try:
        from gibdd_adapter import fetch_deaths_by_month_sync
    except ImportError as exc:
        print(f"[forecast] НЕ удалось импортировать gibdd_adapter: {exc}",
              file=_sys.stderr)
        return {}

    try:
        deaths_by_month, errors = fetch_deaths_by_month_sync(region_code, year)
    except RuntimeError as exc:
        # Скорее всего, вызвано из event loop — нужен async-вариант.
        print(f"[forecast] RuntimeError в fetch_actual_deaths_from_web: {exc}",
              file=_sys.stderr)
        return {}
    except Exception as exc:  # noqa: BLE001
        print(f"[forecast] Ошибка при получении данных с ГИБДД: {exc}",
              file=_sys.stderr)
        return {}

    if errors:
        print(f"[forecast] Получены ошибки от gibdd_adapter "
              f"({len(errors)} шт.): первые = {errors[:2]}",
              file=_sys.stderr)

    # Добиваем нулями месяцы без ДТП (для единообразия UI).
    current_month = TODAY.month
    for m in range(1, current_month + 1):
        deaths_by_month.setdefault(str(m), 0)

    return deaths_by_month


async def fetch_actual_deaths_from_web_async(region_code: str, year: int) -> dict[str, int]:
    """
    Асинхронная версия fetch_actual_deaths_from_web для использования в боте.

    См. документацию синхронной версии.
    """
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).parent))
    from gibdd_adapter import fetch_deaths_by_month

    try:
        deaths_by_month, errors = await fetch_deaths_by_month(region_code, year)
    except Exception as exc:  # noqa: BLE001
        print(f"[forecast] Ошибка при получении данных с ГИБДД (async): {exc}",
              file=_sys.stderr)
        return {}

    if errors:
        print(f"[forecast] Получены ошибки от gibdd_adapter "
              f"({len(errors)} шт.): первые = {errors[:2]}",
              file=_sys.stderr)

    current_month = TODAY.month
    for m in range(1, current_month + 1):
        deaths_by_month.setdefault(str(m), 0)

    return deaths_by_month


def _build_runtime_payload(
    region_code: str,
    deaths_by_month_actual: dict[str, int],
    plan_line_mode: Literal["linear", "horizontal"] = "linear",
) -> dict[str, Any]:
    """
    Сборка итогового payload для UI по уже полученным deaths_by_month_actual.

    Эта функция — общий код для sync- и async-версий runtime_calc.
    """
    current_year = TODAY.year
    current_month = TODAY.month

    # --- История (замороженная или из кэша) ---
    history: dict[str, dict[str, Any]] = {}
    for y in range(2023, current_year):
        rec = get_year_data(region_code, y)
        if rec:
            history[str(y)] = rec

    # --- Текущий год (runtime) ---
    vehicles = load_json_if_exists(DATA_VEHI_DIR / f"{region_code}.json") or {}
    plans = load_json_if_exists(DATA_PLANS_DIR / f"{region_code}.json") or {}

    vehicles_year = vehicles.get("vehicles_by_year", {}).get(str(current_year))
    plan_tr_year = plans.get("plan_tr", {}).get(str(current_year))
    if vehicles_year is None:
        raise RuntimeError(f"Нет Ктс за {current_year} для региона {region_code}")
    if plan_tr_year is None:
        raise RuntimeError(f"Нет плана за {current_year} для региона {region_code}")

    deaths_ytd = sum(deaths_by_month_actual.values())
    deaths_forecast_full = forecast_full_year_deaths(deaths_ytd, current_month)

    tr_actual_ytd = round((deaths_ytd * 10000) / vehicles_year, 3) if deaths_ytd else 0.0
    tr_forecast_full = round((deaths_forecast_full * 10000) / vehicles_year, 3)

    monthly_chart = build_monthly_cumulative_tr(
        deaths_by_month_actual=deaths_by_month_actual,
        deaths_forecast_full_year=deaths_forecast_full,
        vehicles_year=vehicles_year,
        plan_tr_year=plan_tr_year,
        plan_line_mode=plan_line_mode,
        current_month=current_month,
    )

    # --- Серия плана 2023..2030 ---
    plan_series = plans.get("plan_tr", {})

    # --- KPI ---
    deviation_pct = (
        round((tr_forecast_full - plan_tr_year) / plan_tr_year * 100, 1)
        if plan_tr_year > 0 else 0.0
    )
    if deviation_pct <= -5:
        status = "ok"
    elif deviation_pct <= 5:
        status = "warning"
    else:
        status = "danger"

    region_name = (vehicles.get("region_name")
                   or plans.get("region_name")
                   or f"Регион {region_code}")

    return {
        "region": {"code": region_code, "name": region_name},
        "history": history,
        "current_year": {
            "year": current_year,
            "months_actual": list(range(1, current_month + 1)),
            "months_forecast": list(range(current_month + 1, 13)),
            "deaths_by_month_actual": deaths_by_month_actual,
            "deaths_ytd": deaths_ytd,
            "deaths_forecast_full_year": deaths_forecast_full,
            "tr_actual_ytd": tr_actual_ytd,
            "tr_forecast_full_year": tr_forecast_full,
            "tr_plan": plan_tr_year,
            "monthly_chart": monthly_chart,
        },
        "plan_series": plan_series,
        "kpi": {
            "tr_actual_ytd": tr_actual_ytd,
            "tr_forecast_full_year": tr_forecast_full,
            "tr_plan": plan_tr_year,
            "deviation_pct": deviation_pct,
            "status": status,
        },
        "calculated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }


def runtime_calc(region_code: str,
                 plan_line_mode: Literal["linear", "horizontal"] = "linear",
                 ) -> dict[str, Any]:
    """
    Синхронная версия runtime_calc для CLI и тестов.

    ВНИМАНИЕ: использует asyncio.run() через fetch_actual_deaths_from_web.
    НЕ вызывайте из асинхронного контекста (бота) — используйте
    `await runtime_calc_async(...)`.
    """
    deaths_by_month_actual = fetch_actual_deaths_from_web(region_code, TODAY.year)
    return _build_runtime_payload(region_code, deaths_by_month_actual, plan_line_mode)


async def runtime_calc_async(region_code: str,
                             plan_line_mode: Literal["linear", "horizontal"] = "linear",
                             ) -> dict[str, Any]:
    """
    Асинхронная версия runtime_calc для использования в боте.
    """
    deaths_by_month_actual = await fetch_actual_deaths_from_web_async(
        region_code, TODAY.year
    )
    return _build_runtime_payload(region_code, deaths_by_month_actual, plan_line_mode)


# --- Административная команда: пересчёт сезонных коэффициентов ------------


def recalc_seasonal_coefficients() -> dict[str, Any]:
    """
    Пересчитывает seasonal_coefficients.json по имеющейся истории.

    Метод: для каждого месяца m берётся средняя доля погибших в этом
    месяце от годового итога, по всем регионам и годам из data/history/.
    Для каждого региона-года считается вектор из 12 долей, затем они
    усредняются по всем регионам-годам.

    Если в history нет записей с deaths_by_month — фолбэк на default uniform.
    """
    print("[forecast] recalc_seasonal_coefficients: расчёт по истории...")

    # Собираем все записи {region, year, deaths_by_month, deaths_total}.
    samples: list[tuple[dict[str, int], int]] = []
    for hist_file in DATA_HIST_DIR.glob("*.json"):
        with hist_file.open("r", encoding="utf-8") as fh:
            hist = json.load(fh)
        for year_str, rec in hist.get("years", {}).items():
            dbm = rec.get("deaths_by_month")
            total = rec.get("deaths", 0)
            if not dbm or total <= 0:
                continue
            # Нормализуем: убедиться, что все 12 месяцев присутствуют.
            dbm_full = {str(m): int(dbm.get(str(m), 0)) for m in range(1, 13)}
            # Проверим, что сумма по месяцам = годовой итог.
            sum_check = sum(dbm_full.values())
            if sum_check != total:
                # Если расхождение — используем sum_check (он точнее по месяцам).
                pass
            samples.append((dbm_full, sum_check))

    if not samples:
        print("[forecast] В history нет записей с deaths_by_month. "
              "Оставляю текущий файл (default uniform).")
        return load_seasonal_coefficients()

    print(f"[forecast] Найдено {len(samples)} записей "
          f"(регион × год) с месячной разбивкой.")

    # Для каждого месяца считаем среднюю долю.
    monthly_sum = {str(m): 0.0 for m in range(1, 13)}
    for dbm, total in samples:
        for m in range(1, 13):
            monthly_sum[str(m)] += dbm[str(m)] / total

    monthly_share = {
        str(m): round(monthly_sum[str(m)] / len(samples), 4)
        for m in range(1, 13)
    }

    # Нормализуем: сумма должна = 1.0000.
    total_share = sum(monthly_share.values())
    if total_share > 0:
        monthly_share = {
            m: round(v / total_share, 4)
            for m, v in monthly_share.items()
        }

    # Кумулятивные доли.
    cumulative = {}
    running = 0.0
    for m in range(1, 13):
        running += monthly_share[str(m)]
        cumulative[str(m)] = round(running, 4)

    payload = {
        "updated_at": TODAY.isoformat(),
        "method": f"среднее по {len(samples)} регион-годам из data/history/",
        "monthly_share": monthly_share,
        "cumulative_share": cumulative,
    }

    SEASONAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    with SEASONAL_FILE.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)

    print(f"[forecast] {SEASONAL_FILE} обновлён.")
    print(f"[forecast] Сезонный профиль (monthly_share):")
    for m in range(1, 13):
        ms = monthly_share[str(m)]
        cs = cumulative[str(m)]
        bar = "█" * int(ms * 200)
        print(f"  м{m:2d}: {ms:.4f} (cum={cs:.4f}) {bar}")
    return payload


# --- CLI -------------------------------------------------------------------


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--recalc-seasonal", action="store_true",
                        help="Пересчитать seasonal_coefficients.json")
    parser.add_argument("--region", type=str,
                        help="Напечатать runtime_calc для региона")
    parser.add_argument("--plan-line", choices=["linear", "horizontal"],
                        default="linear",
                        help="Режим линии плана на графике 2")
    args = parser.parse_args(argv[1:])

    if args.recalc_seasonal:
        recalc_seasonal_coefficients()
        return 0

    if args.region:
        payload = runtime_calc(args.region, plan_line_mode=args.plan_line)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
