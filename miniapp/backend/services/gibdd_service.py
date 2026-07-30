"""
Сервисный слой Mini App: мост между FastAPI и существующими модулями gibdd-bot.

Все импорты существующих модулей делаются лениво (внутри функций), чтобы:
1. Приложение запускалось даже если какие-то модули ещё не интегрированы.
2. ImportError не валил весь backend, а возвращал понятную ошибку.
3. Можно было тестировать API без реальной выгрузки данных.

Реальные функции gibdd-bot, которые используются:
- user_request_parser.parse_user_message(text) → ParsedRequest
- bot._fetch_cards_for_period(dat_list, reg_code, ...) → (cards, errors)
- gibdd_parser.build_file1_data(cards) / build_file2_data(cards)
- excel_generator.generate_both_files(file1, file2) → (bytes1, bytes2)
- analytics.calculate_metrics(cards) → dict
- report_generator.ReportGenerator(name, period).generate_dtp_map(cards) → HTML
- regions_cache / regions_builtin — список регионов
"""
from __future__ import annotations

import asyncio
import importlib
import logging
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class TaskStatus(str, Enum):
    """Статус асинхронной задачи выгрузки."""

    PENDING = "pending"
    FETCHING = "fetching"
    PARSING = "parsing"
    ANALYTICS = "analytics"
    GENERATING = "generating"
    DONE = "done"
    FAILED = "failed"


@dataclass(slots=True)
class Task:
    """Описание асинхронной задачи."""

    id: str
    user_id: int
    region_code: str
    region_name: str
    period_label: str
    dat_list: List[str]
    raw_query: str
    status: TaskStatus = TaskStatus.PENDING
    progress: int = 0
    error: Optional[str] = None
    files: List[Dict[str, Any]] = field(default_factory=list)
    analytics: Optional[Dict[str, Any]] = None
    total_dtp: int = 0
    total_dead: int = 0
    total_injured: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# In-memory хранилище задач (для production заменить на Redis/PostgreSQL)
_tasks: Dict[str, Task] = {}

# Корень проекта gibdd-bot (находится на 2 уровня выше этого файла):
# miniapp/backend/services/gibdd_service.py → gibdd-bot/
_PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _ensure_project_path() -> None:
    """Добавляет корень gibdd-bot в sys.path (если ещё не добавлен)."""
    root = str(_PROJECT_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)


def _import_module(name: str):
    """Безопасный импорт модуля из gibdd-bot с понятной ошибкой."""
    _ensure_project_path()
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        raise RuntimeError(
            f"Модуль {name} не найден. Убедитесь, что miniapp/ находится "
            f"внутри проекта gibdd-bot (текущий root: {_PROJECT_ROOT}). "
            f"Ошибка: {exc}"
        ) from exc


# ============================================================
# Парсинг запроса пользователя
# ============================================================
async def parse_user_query(query: str) -> Dict[str, Any]:
    """
    Парсит запрос пользователя через существующий user_request_parser.

    Returns:
        {
            "ok": True,
            "region_code": "1101",
            "region_name": "Вологодская область",
            "period": "Январь-Май 2026",
            "dat_list": ["1.2026", "2.2026", ...],
            "raw_query": "..."
        }
        или {"ok": False, "error": "...", "raw_query": "..."}
    """
    try:
        parser = _import_module("user_request_parser")
        result = await parser.parse_user_message(query)

        if result is None:
            return {
                "ok": False,
                "error": "Не удалось распознать регион и период в запросе",
                "raw_query": query,
            }

        return {
            "ok": True,
            "region_code": result.region_code,
            "region_name": result.region_name,
            "period": result.period.label,
            "dat_list": result.period.get_dat_list(),
            "raw_query": query,
        }
    except Exception as exc:
        logger.exception("parse_user_query failed")
        return {
            "ok": False,
            "error": f"Ошибка парсинга: {exc}",
            "raw_query": query,
        }


# ============================================================
# Справочник регионов
# ============================================================
async def get_regions() -> List[Dict[str, Any]]:
    """
    Возвращает список доступных регионов.

    Использует единую функцию ensure_regions_loaded() из user_request_parser,
    которая сама делает: API → файловый кэш → BUILTIN_REGIONS.
    Возвращает список [{'code': '1101', 'name': 'Алтайский край'}, ...].
    """
    try:
        parser = _import_module("user_request_parser")
        regions = await parser.ensure_regions_loaded()
        if regions:
            return regions
    except Exception as exc:
        logger.warning(f"ensure_regions_loaded failed: {exc}, fallback to builtin")

    # Последний fallback — встроенный справочник
    builtin = _import_module("regions_builtin")
    return list(builtin.BUILTIN_REGIONS)


# ============================================================
# Создание и выполнение задач выгрузки
# ============================================================
def create_task(
    user_id: int,
    region_code: str,
    region_name: str,
    period_label: str,
    dat_list: List[str],
    raw_query: str,
) -> Task:
    """Создаёт новую задачу и возвращает её объект."""
    task_id = uuid.uuid4().hex[:12]
    task = Task(
        id=task_id,
        user_id=user_id,
        region_code=region_code,
        region_name=region_name,
        period_label=period_label,
        dat_list=dat_list,
        raw_query=raw_query,
    )
    _tasks[task_id] = task
    return task


def get_task(task_id: str) -> Optional[Task]:
    """Возвращает задачу по ID или None."""
    return _tasks.get(task_id)


def list_user_tasks(user_id: int, limit: int = 20) -> List[Task]:
    """Возвращает последние N задач пользователя."""
    user_tasks = [t for t in _tasks.values() if t.user_id == user_id]
    user_tasks.sort(key=lambda t: t.created_at, reverse=True)
    return user_tasks[:limit]


def _task_dir(task_id: str) -> Path:
    """Директория для файлов задачи (в data/tasks/)."""
    d = _PROJECT_ROOT / "data" / "tasks" / task_id
    d.mkdir(parents=True, exist_ok=True)
    return d


async def execute_task(task_id: str) -> None:
    """
    Асинхронное выполнение задачи выгрузки.

    Шаги:
    1. FETCHING — выгрузка карточек ДТП через bot._fetch_cards_for_period
       (внутри: API → web-fallback → кэш)
    2. PARSING — генерация Excel-данных через gibdd_parser
    3. ANALYTICS — расчёт метрик через analytics.calculate_metrics
    4. GENERATING — запись Excel-файлов и HTML-карты
    """
    task = _tasks.get(task_id)
    if not task:
        return

    try:
        # === 1. FETCHING ===
        task.status = TaskStatus.FETCHING
        task.progress = 10
        task.updated_at = datetime.now(timezone.utc)

        bot_module = _import_module("bot")

        # Используем существующую функцию выгрузки из bot.py.
        # _fetch_cards_for_period уже умеет: API → web_fallback → кэш.
        cards, errors = await bot_module._fetch_cards_for_period(
            dat_list=task.dat_list,
            reg_code=task.region_code,
            log_prefix=f"MiniApp[{task_id}]",
            cache_result=True,
        )

        if errors:
            logger.warning(
                f"Task {task_id}: выгрузка завершена с ошибками: {errors}"
            )

        if not cards:
            task.status = TaskStatus.FAILED
            task.error = (
                "Не удалось получить данные ДТП. "
                f"Ошибки: {'; '.join(errors[:3]) if errors else 'нет данных'}"
            )
            task.updated_at = datetime.now(timezone.utc)
            return

        # Сводная статистика для отображения
        task.total_dtp = len(cards)
        task.total_dead = sum(int(c.get("pog", 0) or 0) for c in cards)
        task.total_injured = sum(int(c.get("ran", 0) or 0) for c in cards)

        # === 2. PARSING ===
        task.status = TaskStatus.PARSING
        task.progress = 45
        task.updated_at = datetime.now(timezone.utc)

        gibdd_parser = _import_module("gibdd_parser")
        file1_data = gibdd_parser.build_file1_data(cards)
        file2_data = gibdd_parser.build_file2_data(cards)

        # === 3. ANALYTICS ===
        task.status = TaskStatus.ANALYTICS
        task.progress = 65
        task.updated_at = datetime.now(timezone.utc)

        try:
            analytics_module = _import_module("analytics")
            task.analytics = analytics_module.calculate_metrics(cards)
        except Exception as exc:
            logger.warning(f"Task {task_id}: analytics failed: {exc}")
            task.analytics = {
                "total_dtp": task.total_dtp,
                "total_dead": task.total_dead,
                "total_injured": task.total_injured,
            }

        # === 4. GENERATING ===
        task.status = TaskStatus.GENERATING
        task.progress = 80
        task.updated_at = datetime.now(timezone.utc)

        out_dir = _task_dir(task_id)
        region_safe = "".join(
            c if c.isalnum() else "_" for c in task.region_name
        )[:30] or task.region_code
        period_safe = "".join(
            c if c.isalnum() else "_" for c in task.period_label
        )[:20]

        # Excel: карточки ДТП + участники (генерируем оба файла одной функцией)
        excel_gen = _import_module("excel_generator")
        file1_bytes, file2_bytes = excel_gen.generate_both_files(
            file1_data, file2_data
        )

        cards_path = out_dir / f"dtp_cards_{region_safe}_{period_safe}.xlsx"
        cards_path.write_bytes(file1_bytes)
        task.files.append({
            "type": "dtp_cards",
            "filename": cards_path.name,
            "path": str(cards_path),
            "size_bytes": len(file1_bytes),
            "mime": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        })

        uch_path = out_dir / f"dtp_uch_{region_safe}_{period_safe}.xlsx"
        uch_path.write_bytes(file2_bytes)
        task.files.append({
            "type": "dtp_participants",
            "filename": uch_path.name,
            "path": str(uch_path),
            "size_bytes": len(file2_bytes),
            "mime": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        })

        # HTML-карта через ReportGenerator
        try:
            report_gen_module = _import_module("report_generator")

            # Подгружаем камеры из кэша, если они есть для этого региона.
            # Файл должен лежать в data/cameras_{reg_code}.xls
            # (загружается через Telegram-бота или через Mini App UI).
            cameras = None
            try:
                camera_cache_module = _import_module("camera_cache")
                if camera_cache_module.has_cached_cameras(task.region_code):
                    cameras = camera_cache_module.load_cameras_from_cache(
                        task.region_code
                    )
                    if cameras:
                        with_pk = sum(
                            1 for c in cameras if c.get("has_piket")
                        )
                        logger.info(
                            f"Task {task_id}: loaded {len(cameras)} cameras "
                            f"({with_pk} with piket) for region "
                            f"{task.region_code}"
                        )
                    else:
                        logger.warning(
                            f"Task {task_id}: camera file exists for "
                            f"{task.region_code} but parser returned empty"
                        )
            except Exception as exc:
                logger.warning(
                    f"Task {task_id}: camera cache load failed: {exc} "
                    f"— building map without cameras"
                )
                cameras = None

            generator = report_gen_module.ReportGenerator(
                region_name=task.region_name,
                period_label=task.period_label,
            )
            html_content = generator.generate_dtp_map(cards, cameras=cameras)

            map_path = out_dir / f"dtp_map_{region_safe}_{period_safe}.html"
            map_path.write_text(html_content, encoding="utf-8")
            task.files.append({
                "type": "map_html",
                "filename": map_path.name,
                "path": str(map_path),
                "size_bytes": len(html_content.encode("utf-8")),
                "mime": "text/html",
            })
        except Exception as exc:
            logger.warning(f"Task {task_id}: map generation failed: {exc}")
            # Карта опциональна — задача считается успешной без неё

        # === DONE ===
        task.status = TaskStatus.DONE
        task.progress = 100
        task.updated_at = datetime.now(timezone.utc)

        logger.info(
            f"Task {task_id} done: {task.total_dtp} ДТП, "
            f"{task.total_dead} погибших, {task.total_injured} раненых, "
            f"{len(task.files)} файлов"
        )

    except Exception as exc:
        logger.exception(f"Task {task_id} failed")
        task.status = TaskStatus.FAILED
        task.error = str(exc)
        task.updated_at = datetime.now(timezone.utc)


# ============================================================
# Статистика по точке
# ============================================================
async def get_point_statistics(
    lat: float, lon: float, radius_km: float
) -> Dict[str, Any]:
    """
    Статистика ДТП в радиусе от заданной точки.

    Реальная функция point_statistics.calculate_point_statistics требует
    уже выгруженные карточки ДТП (current_cards) и радиус в метрах.
    Здесь мы делаем минимальную обёртку: если карточек нет, возвращаем
    заглушку с координатами и радиусом.

    Полноценная реализация потребует предварительной выгрузки данных
    по региону (см. execute_task).
    """
    return {
        "ok": False,
        "error": (
            "Точечная статистика требует предварительно выгруженные карточки "
            "ДТП по региону. Используйте POST /api/dtp/tasks для выгрузки, "
            "затем GET /api/dtp/tasks/{id} для получения analytics."
        ),
        "center": {"lat": lat, "lon": lon},
        "radius_km": radius_km,
        "radius_m": radius_km * 1000,
    }


# ============================================================
# AI-анализ (опционально)
# ============================================================
async def ai_analyze(
    user_id: int,
    query: str,
    context: Dict[str, Any],
) -> Dict[str, Any]:
    """
    AI-анализ данных через ZhipuAI GLM (если задан LLM_API_KEY).

    ВНИМАНИЕ: полная интеграция с llm_analyzer.get_ai_summary требует
    подготовленный comparison-словарь (метрики текущего и прошлого периодов),
    очаги (clusters_context), кросс-таблицы (cross_tables_context) и т.д.
    Эти данные формируются в bot.py через длинную цепочку вызовов.

    Для Mini App MVP возвращаем понятную заглушку. Полноценный AI-анализ
    можно добавить позже, переиспользовав bot._run_ai_analysis() с
    заглушкой для Telegram-зависимых колбэков.
    """
    config = _import_module("config")
    if not config.LLM_API_KEY:
        return {
            "ok": False,
            "error": "LLM_API_KEY не задан — AI-анализ недоступен",
        }

    # Возвращаем базовые сведения о данных — полноценный AI-анализ
    # требует сравнение периодов (current vs prev), что в текущем MVP
    # Mini App ещё не реализовано.
    cards = context.get("cards", [])
    return {
        "ok": False,
        "error": (
            "Полноценный AI-анализ в Mini App будет добавлен позже. "
            "Сейчас он доступен только в Telegram-боте (/dtp → Анализ с ИИ), "
            "т.к. требует сравнение с прошлым периодом и очаги концентрации."
        ),
        "stats": {
            "total_dtp": len(cards),
            "total_dead": sum(int(c.get("pog", 0) or 0) for c in cards),
            "total_injured": sum(int(c.get("ran", 0) or 0) for c in cards),
        },
    }


# ============================================================
# Очистка старых задач (для периодического вызова)
# ============================================================
def cleanup_old_tasks(max_age_hours: int = 24) -> int:
    """
    Удаляет задачи старше max_age_hours.
    Возвращает количество удалённых задач.
    """
    now = datetime.now(timezone.utc)
    cutoff = now.timestamp() - max_age_hours * 3600

    to_delete = [
        tid for tid, task in _tasks.items()
        if task.created_at.timestamp() < cutoff
    ]
    for tid in to_delete:
        task = _tasks.pop(tid, None)
        if task:
            # Удаляем файлы с диска
            for f in task.files:
                try:
                    Path(f["path"]).unlink(missing_ok=True)
                except Exception:
                    pass
            # Удаляем директорию задачи
            try:
                task_dir = _PROJECT_ROOT / "data" / "tasks" / tid
                if task_dir.exists():
                    task_dir.rmdir()
            except Exception:
                pass

    if to_delete:
        logger.info(f"Cleaned up {len(to_delete)} old tasks")
    return len(to_delete)
