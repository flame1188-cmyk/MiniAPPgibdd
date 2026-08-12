"""
core/pipeline_steps.py — compositional steps для Celery-тасков (Sprint 7, Фаза C.3).

Каждая функция — это ОДИН шаг пайплайна выгрузки:
- step_fetch(dat_list, reg_code, log_prefix) → {"ok": bool, "cards": list, "errors": list, "stats": dict}
- step_parse(cards) → {"ok": bool, "file1_data": list, "file2_data": list}
- step_analytics(cards, prev_cards, prev_label) → {"ok": bool, "analytics": dict}
- step_export(file1_data, file2_data, cards, region_name, period_label, ...) →
  {"ok": bool, "file1_bytes_b64": str, "file2_bytes_b64": str, "map_html": str}

Назначение:
  В Фазе C.3 каждый шаг станет отдельным Celery-таском (или группой тасков).
  Промежуточный результат (cards, file1_data, ...) будет сохраняться в Redis (C.4)
  между шагами. Это даёт:
  1. Restartability — упавший на шаге GENERATING таск не перескачивает
     карточки (они уже в Redis), а продолжит с шага GENERATING.
  2. Параллелизм — шаги PARSE и ANALYTICS можно запустить параллельно
     (chord в Celery).
  3. Visibility — каждый шаг логирует свой прогресс, видно в Flower.

  Каждый step_* возвращает dict с полями:
  - "ok": bool — успех/неуспех
  - данные шага (cards, file1_data, analytics, ...)
  - "error": str | None — сообщение об ошибке (если ok=False)
  - "stats": dict — метрики для логирования/мониторинга

Backward compatibility:
  Эти функции НЕ используются в pipeline.execute_task (он остаётся как есть).
  Они — основа для Celery-тасков в Фазе C.3.
"""
from __future__ import annotations

import base64
import logging
from typing import Any, Dict, List, Optional

from .analytics_core import build_analytics_sync
from .exporting import generate_excel_bytes_sync, generate_map_html_sync
from .fetching import fetch_cards_for_period_sync
from .parsing import build_excel_data_sync

logger = logging.getLogger(__name__)


def step_fetch(
    dat_list: List[str],
    reg_code: str,
    log_prefix: str = "Celery[step_fetch]",
) -> Dict[str, Any]:
    """Шаг 1: выгрузка карточек ДТП.

    Returns:
        dict:
        {
            "ok": bool,
            "cards": list[dict],          # пустой если ok=False
            "errors": list[str],          # список предупреждений
            "stats": {
                "total_dtp": int,
                "total_dead": int,
                "total_injured": int,
            },
            "error": str | None,
        }
    """
    try:
        cards, errors = fetch_cards_for_period_sync(
            dat_list=dat_list,
            reg_code=reg_code,
            log_prefix=log_prefix,
            cache_result=True,
        )
        if not cards:
            return {
                "ok": False,
                "cards": [],
                "errors": errors,
                "stats": {"total_dtp": 0, "total_dead": 0, "total_injured": 0},
                "error": (
                    "Не удалось получить данные ДТП. "
                    f"Ошибки: {'; '.join(errors[:3]) if errors else 'нет данных'}"
                ),
            }

        stats = {
            "total_dtp": len(cards),
            "total_dead": sum(int(c.get("pog", 0) or 0) for c in cards),
            "total_injured": sum(int(c.get("ran", 0) or 0) for c in cards),
        }
        logger.info(
            f"{log_prefix}: {stats['total_dtp']} ДТП, "
            f"{stats['total_dead']} погибших, "
            f"{stats['total_injured']} раненых"
        )
        return {
            "ok": True,
            "cards": cards,
            "errors": errors,
            "stats": stats,
            "error": None,
        }
    except Exception as exc:
        logger.exception(f"{log_prefix}: failed")
        return {
            "ok": False,
            "cards": [],
            "errors": [],
            "stats": {"total_dtp": 0, "total_dead": 0, "total_injured": 0},
            "error": str(exc),
        }


def step_parse(
    cards: List[Dict[str, Any]],
    log_prefix: str = "Celery[step_parse]",
) -> Dict[str, Any]:
    """Шаг 2: парсинг карточек в данные для Excel.

    Returns:
        dict:
        {
            "ok": bool,
            "file1_data": list[dict],  # одна строка = одно ДТП
            "file2_data": list[dict],  # одна строка = один участник
            "stats": {"file1_rows": int, "file2_rows": int},
            "error": str | None,
        }
    """
    try:
        file1_data, file2_data = build_excel_data_sync(cards)
        logger.info(
            f"{log_prefix}: file1={len(file1_data)} строк, "
            f"file2={len(file2_data)} строк"
        )
        return {
            "ok": True,
            "file1_data": file1_data,
            "file2_data": file2_data,
            "stats": {
                "file1_rows": len(file1_data),
                "file2_rows": len(file2_data),
            },
            "error": None,
        }
    except Exception as exc:
        logger.exception(f"{log_prefix}: failed")
        return {
            "ok": False,
            "file1_data": [],
            "file2_data": [],
            "stats": {"file1_rows": 0, "file2_rows": 0},
            "error": str(exc),
        }


def step_analytics(
    cards: List[Dict[str, Any]],
    prev_cards: Optional[List[Dict[str, Any]]] = None,
    prev_label: Optional[str] = None,
    current_label: str = "",
    log_prefix: str = "Celery[step_analytics]",
) -> Dict[str, Any]:
    """Шаг 3: расчёт аналитики (метрики, cross-tables, comparison).

    Returns:
        dict:
        {
            "ok": bool,
            "analytics": dict,        # пустой если ok=False
            "stats": {"has_prev_data": bool, "keys": list[str]},
            "error": str | None,
        }
    """
    try:
        analytics = build_analytics_sync(
            cards=cards,
            prev_cards=prev_cards,
            prev_label=prev_label,
        )
        if current_label and isinstance(analytics, dict):
            analytics["current_label"] = current_label

        logger.info(
            f"{log_prefix}: analytics built — "
            f"current={len(cards)} ДТП, "
            f"prev={'нет' if not prev_cards else f'{len(prev_cards)} ДТП'}, "
            f"keys={list(analytics.keys())[:6]}..."
        )
        return {
            "ok": True,
            "analytics": analytics,
            "stats": {
                "has_prev_data": bool(prev_cards),
                "keys": list(analytics.keys()) if isinstance(analytics, dict) else [],
            },
            "error": None,
        }
    except Exception as exc:
        logger.exception(f"{log_prefix}: failed")
        return {
            "ok": False,
            "analytics": {},
            "stats": {"has_prev_data": bool(prev_cards), "keys": []},
            "error": str(exc),
        }


def step_export(
    file1_data: List[Dict[str, Any]],
    file2_data: List[Dict[str, Any]],
    cards: List[Dict[str, Any]],
    region_name: str,
    period_label: str,
    cameras: Optional[List[Dict[str, Any]]] = None,
    prev_cards: Optional[List[Dict[str, Any]]] = None,
    prev_label: Optional[str] = None,
    log_prefix: str = "Celery[step_export]",
) -> Dict[str, Any]:
    """Шаг 4: генерация Excel-байтов и HTML-карты.

    Excel-байты возвращаются как base64-строки — Celery требует JSON-сериализуемых
    результатов. Celery-таск (C.3) декодирует base64 и запишет файлы на диск
    (или загрузит в S3).

    Returns:
        dict:
        {
            "ok": bool,
            "file1_bytes_b64": str,    # base64(excel_bytes)
            "file2_bytes_b64": str,    # base64(excel_bytes)
            "file1_size": int,         # размер в байтах
            "file2_size": int,
            "map_html": str,           # HTML-карта (может быть пустой при ошибке)
            "stats": {"file1_kb": int, "file2_kb": int, "map_kb": int},
            "error": str | None,
        }
    """
    try:
        # === Excel ===
        file1_bytes, file2_bytes = generate_excel_bytes_sync(file1_data, file2_data)

        # === HTML-карта ===
        map_html = ""
        try:
            map_html = generate_map_html_sync(
                cards=cards,
                region_name=region_name,
                period_label=period_label,
                cameras=cameras,
                prev_cards=prev_cards,
                prev_label=prev_label,
            )
        except Exception as exc:
            # Карта опциональна — задача считается успешной без неё
            logger.warning(f"{log_prefix}: map generation failed: {exc}")
            map_html = ""

        file1_b64 = base64.b64encode(file1_bytes).decode("ascii")
        file2_b64 = base64.b64encode(file2_bytes).decode("ascii")

        stats = {
            "file1_kb": len(file1_bytes) // 1024,
            "file2_kb": len(file2_bytes) // 1024,
            "map_kb": len(map_html.encode("utf-8")) // 1024,
        }
        logger.info(
            f"{log_prefix}: export done — "
            f"file1={stats['file1_kb']} KB, file2={stats['file2_kb']} KB, "
            f"map={stats['map_kb']} KB"
        )
        return {
            "ok": True,
            "file1_bytes_b64": file1_b64,
            "file2_bytes_b64": file2_b64,
            "file1_size": len(file1_bytes),
            "file2_size": len(file2_bytes),
            "map_html": map_html,
            "stats": stats,
            "error": None,
        }
    except Exception as exc:
        logger.exception(f"{log_prefix}: failed")
        return {
            "ok": False,
            "file1_bytes_b64": "",
            "file2_bytes_b64": "",
            "file1_size": 0,
            "file2_size": 0,
            "map_html": "",
            "stats": {"file1_kb": 0, "file2_kb": 0, "map_kb": 0},
            "error": str(exc),
        }
