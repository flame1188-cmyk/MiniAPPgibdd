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
- report_generator.ReportGenerator(region_name, period_label).generate_dtp_map(cards) → HTML
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


class AnalysisStatus(str, Enum):
    """Статус длительной аналитической операции (очаги/LLM)."""

    IDLE = "idle"              # ещё не запускали
    RUNNING = "running"        # выполняется
    DONE = "done"              # готово
    FAILED = "failed"          # ошибка


@dataclass
class AnalysisState:
    """Состояние длительной аналитической операции.

    Хранится прямо в Task, чтобы переиспользовать результат
    при повторном открытии вкладки (без пересчёта).
    """

    status: AnalysisStatus = AnalysisStatus.IDLE
    progress: int = 0
    stage: str = ""            # человекочитаемая стадия
    result: Optional[Any] = None
    error: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    def reset(self) -> None:
        self.status = AnalysisStatus.IDLE
        self.progress = 0
        self.stage = ""
        self.result = None
        self.error = None
        self.started_at = None
        self.finished_at = None


@dataclass
class Task:
    """Описание асинхронной задачи выгрузки."""

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

    # === Persisted data for downstream analysis ===
    # Сырые карточки ДТП текущего периода — нужны для очагов, точки, LLM
    cards: List[Dict[str, Any]] = field(default_factory=list)

    # Карточки за прошлый год (lazy-loaded через _ensure_prev_cards)
    prev_cards: List[Dict[str, Any]] = field(default_factory=list)
    prev_label: Optional[str] = None
    prev_cards_loaded: bool = False

    # Сравнение метрик (current vs prev) — нужно для LLM
    comparison: Optional[Dict[str, Any]] = None

    # Состояния длительных операций
    clusters_state: AnalysisState = field(default_factory=AnalysisState)
    llm_summary_state: AnalysisState = field(default_factory=AnalysisState)

    # История вопросов LLM (последние 10)
    llm_qa_history: List[Dict[str, str]] = field(default_factory=list)

    # Кэш: последняя точечная статистика (для отображения без пересчёта)
    last_point_stats: Optional[Dict[str, Any]] = None

    # === Raw данные для Excel-выгрузки и продвинутой карты ===
    # Полные объекты очагов (с cards внутри) — не проходят через JSON-API,
    # но нужны для generate_cluster_map() и generate_concentration_dynamics_file()
    raw_clusters: List[Dict[str, Any]] = field(default_factory=list)
    # Полные объекты предочагов — сохраняются ОТДЕЛЬНО, потому что
    # предочаги могут существовать даже когда очагов нет (малые регионы).
    # Ранее предочаги прикреплялись к clusters[0]["_preclusters"] и терялись
    # при пустом списке очагов — это приводило к пустой карте и 500 в Excel.
    raw_preclusters: List[Dict[str, Any]] = field(default_factory=list)
    # Полные карточки последнего point stats запроса (с _dist_m) — для Excel
    last_point_cards_current: List[Dict[str, Any]] = field(default_factory=list)
    last_point_cards_prev: List[Dict[str, Any]] = field(default_factory=list)
    last_point_params: Optional[Dict[str, Any]] = None  # {lat, lon, radius_m}


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
    """Создаёт новую задачу и возвращает её объект.

    Задача сохраняется:
    - В in-memory _tasks (для совместимости со старым кодом)
    - В БД через repository.save_task (если DATABASE_URL задан)
    """
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

    # Асинхронно сохраняем в БД (если доступна).
    # Fire-and-forget — задача уже в in-memory и доступна сразу.
    try:
        from ..db.repository import save_task
        asyncio.create_task(save_task(task))
    except Exception as exc:
        logger.debug(f"create_task: DB save skipped: {exc}")

    return task


async def get_task_async(task_id: str) -> Optional[Task]:
    """Асинхронная версия get_task — проверяет и БД, и in-memory."""
    # Сначала in-memory (быстро + есть тяжёлые поля)
    if task_id in _tasks:
        return _tasks[task_id]

    # Потом БД (если есть)
    try:
        from ..db.connection import is_db_ready
        from ..db.repository import load_task, attach_heavy_state
        if not is_db_ready():
            return None
        task = await load_task(task_id, _task_factory)
        if task is not None:
            attach_heavy_state(task)
            _tasks[task_id] = task  # кэшируем
        return task
    except Exception as exc:
        logger.debug(f"get_task_async: DB load failed: {exc}")
        return _tasks.get(task_id)


def get_task(task_id: str) -> Optional[Task]:
    """Возвращает задачу по ID или None (синхронная версия).

    ВНИМАНИЕ: проверяет только in-memory кэш. Если задача существует
    только в БД (например, после рестарта процесса) — вернёт None.
    Используйте get_task_async() для полной проверки (БД + memory).
    """
    return _tasks.get(task_id)


def _task_factory(
    id: str,
    user_id: int,
    region_code: str,
    region_name: str,
    period_label: str,
    dat_list: List[str],
    raw_query: str,
) -> Task:
    """Фабрика Task для repository.load_task (без циклического импорта)."""
    return Task(
        id=id,
        user_id=user_id,
        region_code=region_code,
        region_name=region_name,
        period_label=period_label,
        dat_list=dat_list,
        raw_query=raw_query,
    )


async def list_user_tasks(user_id: int, limit: int = 20) -> List[Task]:
    """Возвращает последние N задач пользователя.

    При наличии БД — из БД (consistent между воркерами).
    Иначе — из in-memory _tasks.
    """
    # Сначала in-memory (быстро + содержит тяжёлые поля)
    user_tasks_in_memory = [
        t for t in _tasks.values() if t.user_id == user_id
    ]
    user_tasks_in_memory.sort(key=lambda t: t.created_at, reverse=True)

    # Проверяем готовность БД (lazy import чтобы избежать циклов)
    try:
        from ..db.connection import is_db_ready
    except Exception:
        is_db_ready = lambda: False  # noqa: E731

    if not is_db_ready():
        return user_tasks_in_memory[:limit]

    try:
        from ..db.repository import list_user_tasks_from_db, attach_heavy_state
        db_tasks = await list_user_tasks_from_db(user_id, limit, _task_factory)
        # Присоединяем тяжёлые поля из кэша (если есть)
        for t in db_tasks:
            attach_heavy_state(t)

        # Если в БД задач больше, чем в памяти (например, после рестарта) —
        # дополняем список из БД. Если в памяти есть задача, которой нет в БД
        # (например, только что создана, save_task ещё не завершился) —
        # включаем её в результат, убирая дубли.
        seen_ids = {t.id for t in db_tasks}
        for t in user_tasks_in_memory:
            if t.id not in seen_ids:
                db_tasks.insert(0, t)  # свежие — первыми
        return db_tasks[:limit]
    except Exception as exc:
        logger.debug(f"list_user_tasks: DB query failed: {exc}")
        return user_tasks_in_memory[:limit]


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

    На каждом переходе статуса — сохранение в БД через repository.save_task
    (если DATABASE_URL задан; иначе работает только in-memory).
    """
    task = _tasks.get(task_id)
    if not task:
        # Возможно, задача создана в другом воркере и есть в БД.
        task = await get_task_async(task_id)
        if not task:
            return
        _tasks[task_id] = task

    # Локальный helper дляpersist-апдейтов
    async def _persist() -> None:
        try:
            from ..db.repository import save_task
            await save_task(task)
        except Exception as exc:
            logger.debug(f"execute_task: persist failed: {exc}")

    try:
        # === 1. FETCHING ===
        task.status = TaskStatus.FETCHING
        task.progress = 10
        task.updated_at = datetime.now(timezone.utc)
        await _persist()

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
            await _persist()
            return

        # Сводная статистика для отображения
        task.total_dtp = len(cards)
        task.total_dead = sum(int(c.get("pog", 0) or 0) for c in cards)
        task.total_injured = sum(int(c.get("ran", 0) or 0) for c in cards)

        # Сохраняем сырые карточки для последующего анализа
        # (очаги, статистика по точке, LLM-анализ)
        task.cards = cards

        # === 2. PARSING ===
        task.status = TaskStatus.PARSING
        task.progress = 45
        task.updated_at = datetime.now(timezone.utc)
        await _persist()

        gibdd_parser = _import_module("gibdd_parser")
        file1_data = gibdd_parser.build_file1_data(cards)
        file2_data = gibdd_parser.build_file2_data(cards)

        # === 3. ANALYTICS ===
        task.status = TaskStatus.ANALYTICS
        task.progress = 65
        task.updated_at = datetime.now(timezone.utc)
        await _persist()

        try:
            analytics_module = _import_module("analytics")

            # Лучше-эффort загрузка карточек прошлого года для сравнения АППГ.
            # Если prev_cards не загрузились — analytics всё равно валиден,
            # но без блока comparison/previous.
            try:
                if not task.prev_cards_loaded:
                    await ensure_prev_cards(task)
                prev_cards = task.prev_cards or []
                prev_label = task.prev_label
            except Exception as exc:
                logger.warning(
                    f"Task {task_id}: prev_cards load for analytics failed: "
                    f"{exc} — analytics without comparison"
                )
                prev_cards = []
                prev_label = None

            task.analytics = analytics_module.build_full_analytics(
                cards,
                prev_cards if prev_cards else None,
                prev_label,
            )
            # Добавляем current_label для UI
            if isinstance(task.analytics, dict):
                task.analytics["current_label"] = task.period_label
            logger.info(
                f"Task {task_id}: analytics built — "
                f"current={len(cards)} ДТП, "
                f"prev={'нет' if not prev_cards else f'{len(prev_cards)} ДТП'}"
            )
        except Exception as exc:
            logger.warning(f"Task {task_id}: analytics failed: {exc}")
            task.analytics = {
                "total_dtp": task.total_dtp,
                "total_dead": task.total_dead,
                "total_injured": task.total_injured,
                "has_prev_data": False,
            }
        await _persist()  # сохраняем analytics в БД

        # === 4. GENERATING ===
        task.status = TaskStatus.GENERATING
        task.progress = 80
        task.updated_at = datetime.now(timezone.utc)
        await _persist()

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
            # Передаём карточки прошлого года, чтобы на карте появилась
            # динамика АППГ в верхней сводке (ДТП / Погибшие / Раненые).
            prev_cards_for_map = task.prev_cards or None
            html_content = generator.generate_dtp_map(
                cards,
                cameras=cameras,
                prev_cards=prev_cards_for_map,
                prev_label=task.prev_label,
            )

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
        await _persist()

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
        await _persist()


# ============================================================
# Загрузка данных за прошлый год (lazy)
# ============================================================
async def ensure_prev_cards(task: Task) -> Dict[str, Any]:
    """
    Гарантирует, что task.prev_cards загружены.

    Возвращает:
        {
            "ok": True,
            "prev_cards": [...],
            "prev_label": "...",
        }
        или {"ok": False, "error": "..."}
    """
    if task.prev_cards_loaded:
        return {
            "ok": bool(task.prev_cards),
            "prev_cards": task.prev_cards,
            "prev_label": task.prev_label or "",
            "error": None if task.prev_cards else "Нет данных за прошлый год",
        }

    # Вычисляем прошлый период: те же месяцы, год-1
    # dat_list = ['1.2026', '2.2026', ...] -> ['1.2025', '2.2025', ...]
    prev_dat_list = []
    for dat in task.dat_list:
        try:
            m, y = dat.split(".")
            prev_dat_list.append(f"{m}.{int(y) - 1}")
        except Exception:
            continue

    if not prev_dat_list:
        task.prev_cards_loaded = True
        return {"ok": False, "error": "Не удалось вычислить прошлый период"}

    # Формируем label прошлого периода
    try:
        year = int(task.dat_list[0].split(".")[1])
        prev_year = year - 1
        prev_label = task.period_label.replace(str(year), str(prev_year))
    except Exception:
        prev_label = f"Прошлый период ({prev_dat_list[0]})"

    try:
        bot_module = _import_module("bot")
        prev_cards, errors = await bot_module._fetch_cards_for_period(
            dat_list=prev_dat_list,
            reg_code=task.region_code,
            log_prefix=f"MiniApp[{task.id}]/prev",
            cache_result=True,
        )
        task.prev_cards = prev_cards or []
        task.prev_label = prev_label
        task.prev_cards_loaded = True

        if errors:
            logger.warning(
                f"Task {task.id}: prev cards loaded with errors: {errors}"
            )

        return {
            "ok": bool(task.prev_cards),
            "prev_cards": task.prev_cards,
            "prev_label": prev_label,
            "error": None if task.prev_cards else (
                f"Нет данных за прошлый год ({prev_label}). "
                f"Возможно, данные ещё не опубликованы."
            ),
        }
    except Exception as exc:
        logger.exception(f"Task {task.id}: ensure_prev_cards failed")
        task.prev_cards_loaded = True  # не пытаемся снова
        return {"ok": False, "error": str(exc)}


# ============================================================
# Сравнение метрик (для LLM)
# ============================================================
async def ensure_comparison(task: Task) -> Dict[str, Any]:
    """
    Гарантирует, что task.comparison посчитан.

    Сравнение = текущие метрики vs метрики прошлого года.
    Если данных за прошлый год нет — comparison содержит только current.
    """
    if task.comparison is not None:
        return {"ok": True, "comparison": task.comparison}

    if not task.cards:
        return {"ok": False, "error": "Карточки текущего периода не загружены"}

    analytics_module = _import_module("analytics")
    current_metrics = analytics_module.calculate_metrics(task.cards)

    # Грузим прошлый год
    prev_result = await ensure_prev_cards(task)
    prev_metrics = None
    if prev_result.get("ok") and prev_result.get("prev_cards"):
        prev_metrics = analytics_module.calculate_metrics(
            prev_result["prev_cards"]
        )

    if prev_metrics:
        comparison = analytics_module.compare_metrics(
            current_metrics, prev_metrics
        )
    else:
        # Нет прошлого года — формируем урезанный comparison
        comparison = {
            "total": {"current": current_metrics.get("total", 0),
                      "previous": 0, "change": 0},
            "deaths": {"current": current_metrics.get("deaths", 0),
                       "previous": 0, "change": 0},
            "injured": {"current": current_metrics.get("injured", 0),
                        "previous": 0, "change": 0},
            "alcohol": {"current": current_metrics.get("alcohol", 0),
                        "previous": 0, "change": 0},
            "pedestrians": {"current": current_metrics.get("pedestrians", 0),
                            "previous": 0, "change": 0},
            "deaths_per_100": {
                "current": current_metrics.get("deaths_per_100", 0),
                "previous": 0, "change": 0,
            },
            "injured_per_100": {
                "current": current_metrics.get("injured_per_100", 0),
                "previous": 0, "change": 0,
            },
            "by_weekday": {"current": current_metrics.get("by_weekday", {}),
                           "previous": {}},
            "by_hour": {"current": current_metrics.get("by_hour", {}),
                        "previous": {}},
            "by_type": {"current": current_metrics.get("by_type", {}),
                        "previous": {}},
            "by_weather": {"current": current_metrics.get("by_weather", {}),
                           "previous": {}},
        }

    task.comparison = comparison
    return {"ok": True, "comparison": comparison}


# ============================================================
# Статистика по точке
# ============================================================
async def compute_point_stats(
    task: Task,
    lat: float,
    lon: float,
    radius_m: int,
) -> Dict[str, Any]:
    """
    Считает статистику ДТП в радиусе от точки.

    Использует point_statistics.calculate_point_statistics.
    Требует загруженные карточки (task.cards).

    Returns:
        {
            "ok": True,
            "center": {"lat": ..., "lon": ...},
            "radius_m": ...,
            "current": {total, deaths, injured, alcohol, pedestrians,
                        by_type, by_road, by_weather, cards},
            "prev": {...} | null,
            "prev_label": "...",
            "current_label": "...",
        }
    """
    if not task.cards:
        return {"ok": False, "error": "Карточки текущего периода не загружены"}

    point_stats_module = _import_module("point_statistics")

    # Загружаем прошлый год (если ещё нет)
    prev_cards = []
    prev_label = ""
    if not task.prev_cards_loaded:
        await ensure_prev_cards(task)
    prev_cards = task.prev_cards or []
    prev_label = task.prev_label or ""

    stats = await asyncio.to_thread(
        point_stats_module.calculate_point_statistics,
        lat, lon, radius_m,
        task.cards,
        prev_cards if prev_cards else None,
    )

    # Сериализуем: убираем непередаваемые объекты (Counter уже dict)
    def _serialize_period(p: dict) -> dict:
        if not p:
            return None
        return {
            "total": p.get("total", 0),
            "deaths": p.get("deaths", 0),
            "injured": p.get("injured", 0),
            "alcohol": p.get("alcohol", 0),
            "pedestrians": p.get("pedestrians", 0),
            "by_type": dict(p.get("by_type", {})),
            "by_road": dict(p.get("by_road", {})),
            "by_weather": dict(p.get("by_weather", {})),
            # Не возвращаем cards целиком — только количество и первые 5
            # для отображения. Полный список доступен через Excel-выгрузку.
            "cards_count": len(p.get("cards", [])),
            "cards_preview": [
                {
                    "date": str(c.get("date_dtp", "")),
                    "time": str(c.get("time", "")),
                    "type": str(c.get("dtpv", "")),
                    "road": str(c.get("dor", "") or c.get("street", "")),
                    "deaths": int(c.get("pog", 0) or 0),
                    "injured": int(c.get("ran", 0) or 0),
                    "dist_m": round(float(c.get("_dist_m", 0)), 1),
                    "lat": float(str(c.get("coord_w", "0")).strip() or 0),
                    "lon": float(str(c.get("coord_l", "0")).strip() or 0),
                }
                for c in (p.get("cards") or [])[:20]
            ],
        }

    result = {
        "ok": True,
        "center": {"lat": lat, "lon": lon},
        "radius_m": radius_m,
        "current_label": task.period_label,
        "prev_label": prev_label if prev_cards else None,
        "current": _serialize_period(stats.get("current")),
        "prev": _serialize_period(stats.get("prev")) if prev_cards else None,
    }

    # Кэшируем на задаче для повторного отображения
    task.last_point_stats = result
    # Сохраняем карточки (с _dist_m) для Excel-выгрузки
    cur = (stats.get("current") or {})
    prev = (stats.get("prev") or {})
    task.last_point_cards_current = list(cur.get("cards", []) or [])
    task.last_point_cards_prev = list(prev.get("cards", []) or []) if prev_cards else []
    task.last_point_params = {"lat": lat, "lon": lon, "radius_m": radius_m}
    return result


# ============================================================
# Очаги концентрации ДТП
# ============================================================
async def start_clusters_calculation(task: Task) -> None:
    """
    Асинхронный расчёт очагов концентрации ДТП.

    Длительная операция (15-30 сек): OSM Overpass + классификация +
    кластеризация + динамика vs прошлый год.

    Результат сохраняется в task.clusters_state.result.
    """
    state = task.clusters_state
    state.status = AnalysisStatus.RUNNING
    state.progress = 5
    state.stage = "Подготовка данных..."
    state.started_at = datetime.now(timezone.utc)
    state.error = None
    state.result = None

    try:
        if not task.cards:
            raise RuntimeError("Карточки текущего периода не загружены")

        # === Этап 4: проверяем кэш очагов в PostgreSQL ===
        # Если для данного (reg_code, current_dat, prev_dat) уже есть
        # свежий result — берём его без пересчёта (15-30 сек → <100 мс).
        # prev_dat_list вычисляем заранее, чтобы ключ кэша совпал с тем,
        # что будет использоваться при PUT в конце функции.
        try:
            prev_dat_list_for_cache: List[str] = []
            for dat in task.dat_list:
                try:
                    m, y = dat.split(".")
                    prev_dat_list_for_cache.append(f"{m}.{int(y) - 1}")
                except Exception:
                    continue

            from ..db.clusters_cache import get_cached_clusters
            cached_result = await get_cached_clusters(
                reg_code=task.region_code,
                current_dat_list=task.dat_list,
                prev_dat_list=prev_dat_list_for_cache if prev_dat_list_for_cache else None,
            )
            if cached_result is not None:
                # Кэш хит — подставляем result и выходим.
                # raw_clusters остаются пустыми — они нужны только для
                # Excel-выгрузки и продвинутой карты, при их запросе
                # будет сделан перерасчёт (cache miss).
                task.clusters_state.result = cached_result
                task.clusters_state.status = AnalysisStatus.DONE
                task.clusters_state.progress = 100
                task.clusters_state.stage = "Готово (из кэша)"
                task.clusters_state.started_at = datetime.now(timezone.utc)
                task.clusters_state.finished_at = datetime.now(timezone.utc)

                logger.info(
                    f"Task {task.id}: clusters loaded from cache — "
                    f"{cached_result.get('total_clusters', 0)} очагов, "
                    f"{cached_result.get('total_preclusters', 0)} предочагов"
                )
                return
        except Exception as exc:
            logger.debug(
                f"Task {task.id}: clusters cache lookup failed: {exc}"
            )
            # Не роняем расчёт — просто идём штатным путём.

        conc_module = _import_module("concentration_points")

        # Загружаем прошлый год (если ещё нет)
        state.progress = 10
        state.stage = "Загрузка данных за прошлый год..."
        if not task.prev_cards_loaded:
            await ensure_prev_cards(task)
        prev_cards = task.prev_cards or []

        async def progress_cb(text: str) -> None:
            state.stage = text
            state.progress = min(85, state.progress + 5)

        state.progress = 20
        state.stage = "Загрузка границ НП из OpenStreetMap..."

        clusters, _saved_polys, preclusters_raw = await conc_module.calculate_concentration_dynamics(
            current_cards=task.cards,
            prev_cards=prev_cards,
            progress_callback=progress_cb,
            reg_code=task.region_code,
        )

        state.progress = 90
        state.stage = "Обогащение камерами..."

        # Обогащение камерами (если есть в кэше)
        try:
            camera_cache_module = _import_module("camera_cache")
            if camera_cache_module.has_cached_cameras(task.region_code):
                cameras = camera_cache_module.load_cameras_from_cache(
                    task.region_code
                )
                if cameras:
                    current_only = [
                        c for c in clusters if not c.get("_is_lost", False)
                    ]
                    conc_module.enrich_clusters_with_cameras(
                        current_only, cameras,
                    )
                    lost = [
                        c for c in clusters if c.get("_is_lost", False)
                    ]
                    if lost:
                        conc_module.enrich_clusters_with_cameras(lost, cameras)
                    # Предочаги тоже обогащаем камерами — раньше это делалось
                    # только в Telegram-боте (bot.py:2574), а в MiniApp
                    # пропускалось. Без этого пользователь MiniApp видел
                    # предочаги без статуса «закрыт/открыт камерой».
                    if preclusters_raw:
                        conc_module.enrich_clusters_with_cameras(
                            preclusters_raw, cameras,
                        )
                        logger.info(
                            f"Task {task.id}: cameras attached to "
                            f"{len(preclusters_raw)} preclusters"
                        )
        except Exception as exc:
            logger.warning(
                f"Task {task.id}: camera enrichment failed: {exc}"
            )

        state.progress = 95
        state.stage = "Формирование результата..."

        # Сериализуем очаги для JSON-ответа
        clusters_data = [_serialize_cluster(c) for c in clusters]

        # Статистика
        # current_only — текущие очаги (без lost и без prev_matched,
        # т.к. prev_matched — это очаг АППГ, повторённый в текущем;
        # он показывается отдельной строкой, но не входит в «текущие»).
        current_only = [
            c for c in clusters
            if not c.get("_is_lost", False) and not c.get("_is_prev_matched", False)
        ]
        lost_clusters = [c for c in clusters if c.get("_is_lost", False)]
        prev_matched_clusters = [c for c in clusters if c.get("_is_prev_matched", False)]

        # Динамика — агрегат по всем статусам.
        # Новые ключи (методология пикетаж + сосед):
        #   repeated_growing, repeated_shrinking, repeated_stable, repeated_merged,
        #   new, new_with_neighbor, prev_matched, lost
        # prev_matched — это очаг прошлого года, который повторился в текущем
        # (отдельная строка в Excel/карте со ссылкой на текущий №).
        # Старые ключи (growing/shrinking/stable) оставлены для обратной совместимости
        # с сохранёнными задачами и старым фронтендом.
        dynamics_summary = {
            "repeated_growing": 0,
            "repeated_shrinking": 0,
            "repeated_stable": 0,
            "repeated_merged": 0,
            "new": 0,
            "new_with_neighbor": 0,
            "prev_matched": 0,
            "lost": 0,
        }
        for c in clusters:
            d = c.get("dynamics") or {}
            status = d.get("status", "new")
            if status in dynamics_summary:
                dynamics_summary[status] += 1
            else:
                # Неизвестный статус — добавим динамически,
                # чтобы не потерять данные при будущих изменениях.
                dynamics_summary[status] = 1

        # Предочаги — приходят отдельно от calculate_concentration_dynamics,
        # чтобы они не терялись, когда очагов нет (малые регионы).
        preclusters_raw = preclusters_raw or []
        # Backward-compat: предочаги всё ещё прикреплены к clusters[0]["_preclusters"]
        # когда clusters непустой (см. concentration_points.py).
        preclusters = [_serialize_cluster(p) for p in preclusters_raw]

        result = {
            "total_clusters": len(current_only),
            "total_lost": len(lost_clusters),
            "total_prev_matched": len(prev_matched_clusters),
            "total_preclusters": len(preclusters),
            "current_total_dtp": sum(
                c.get("total_accidents", 0) for c in current_only
            ),
            "current_deaths": sum(
                c.get("deaths", 0) for c in current_only
            ),
            "current_injured": sum(
                c.get("injured", 0) for c in current_only
            ),
            "dynamics": dynamics_summary,
            "clusters": clusters_data,
            "preclusters": preclusters,
            "has_prev_data": bool(prev_cards),
            "prev_label": task.prev_label if prev_cards else None,
            "current_label": task.period_label,
            "region_name": task.region_name,
            # Карта очагов будет сгенерирована отдельно по запросу
        }

        state.result = result
        # Сохраняем raw очаги (с cards) для Excel-выгрузки и продвинутой карты
        task.raw_clusters = clusters
        # Сохраняем raw предочаги отдельно — на случай, когда очагов нет,
        # но предочаги есть (нужно для Excel и карты)
        task.raw_preclusters = preclusters_raw
        state.status = AnalysisStatus.DONE
        state.progress = 100
        state.stage = "Готово"
        state.finished_at = datetime.now(timezone.utc)

        # === Этап 4: сохраняем result в кэш очагов ===
        # Ключ: (reg_code, current_dat_hash, prev_dat_hash).
        # prev_dat_list вычислен в начале функции (prev_dat_list_for_cache).
        # Если совпадёт с повторным запросом — следующий пользователь
        # получит результат мгновенно (cache hit).
        try:
            from ..db.clusters_cache import put_cached_clusters
            await put_cached_clusters(
                reg_code=task.region_code,
                current_dat_list=task.dat_list,
                prev_dat_list=prev_dat_list_for_cache if prev_dat_list_for_cache else None,
                result=result,
            )
        except Exception as exc:
            logger.debug(
                f"Task {task.id}: clusters cache put failed: {exc}"
            )

        # Персистим clusters_result в БД (чтобы пережил рестарт)
        try:
            from ..db.repository import save_task
            await save_task(task)
        except Exception as exc:
            logger.debug(f"Task {task.id}: clusters persist failed: {exc}")

        logger.info(
            f"Task {task.id}: clusters done — "
            f"{len(current_only)} очагов, "
            f"{len(prev_matched_clusters)} АППГ-повторённых, "
            f"{len(preclusters)} предочагов, "
            f"{len(lost_clusters)} исчезнувших"
        )

    except Exception as exc:
        logger.exception(f"Task {task.id}: clusters calculation failed")
        state.status = AnalysisStatus.FAILED
        state.error = str(exc)
        state.stage = "Ошибка"
        state.finished_at = datetime.now(timezone.utc)
        # Персистим failed-статус
        try:
            from ..db.repository import save_task
            await save_task(task)
        except Exception:
            pass


def _serialize_cluster(c: dict) -> dict:
    """Сериализует очаг в JSON-совместимый dict."""
    center = c.get("center")
    return {
        "road": c.get("road", ""),
        "zone_type": c.get("zone_type", ""),
        "total_accidents": c.get("total_accidents", 0),
        "deaths": c.get("deaths", 0),
        "injured": c.get("injured", 0),
        # None (смешанный тип, 5+ ДТП разных видов) -> пустая строка для UI
        "dominant_type": c.get("dominant_type") or "",
        "type_counter": dict(c.get("type_counter", {})),
        "center": {"lat": center[0], "lon": center[1]} if center else None,
        "start_pos": c.get("start_pos"),
        "end_pos": c.get("end_pos"),
        "dates": c.get("dates", []),
        # dynamics теперь содержит расширенные поля:
        # - status: repeated_*/new/new_with_neighbor/prev_matched/lost
        # - matched_prev_numbers: [int, ...] — для ссылки «Да, №N»
        # - matched_curr_numbers: [int, ...] — для prev_matched,
        #   на какие текущие № ссылается этот АППГ-очаг
        # - neighbors: [{prev_number, distance_m}, ...] — для new_with_neighbor
        # - prev_total, prev_deaths, prev_injured — суммы по сматченным
        "dynamics": c.get("dynamics", {}),
        "camera_match": c.get("camera_match"),
        # Флаги для фильтрации на фронтенде/карте
        "is_lost": c.get("_is_lost", False),
        "is_prev_matched": c.get("_is_prev_matched", False),
    }


async def generate_clusters_map_html(task: Task) -> Optional[str]:
    """
    Генерирует HTML-карту очагов через ReportGenerator.generate_cluster_map().

    Полноценная карта из Telegram-бота:
      - Слои (Очаги / ДТП в очагах / Предочаги / Камеры)
      - Popups на каждом ДТП и очаге
      - Линейка для измерения расстояний
      - Фильтр камер по моделям
      - Convex hull (зона очага)
      - Динамика (новые/рост/снижение/стабильный/исчезнувший)
      - Камеры с кластеризацией
    """
    if not task.clusters_state.result:
        return None

    try:
        report_gen_module = _import_module("report_generator")
        camera_cache_module = _import_module("camera_cache")

        # Raw очаги с cards внутри (сохранены в start_clusters_calculation)
        raw_clusters = task.raw_clusters or []
        # Raw предочаги (сохранены отдельно — могут быть, даже если очагов нет)
        raw_preclusters = task.raw_preclusters or []
        if not raw_clusters and not raw_preclusters:
            # Нет ни очагов, ни предочагов — простая карта покажет заглушку
            logger.warning(
                f"Task {task.id}: raw_clusters and raw_preclusters empty, "
                f"fallback to simple map"
            )
            return _build_clusters_map_html(task)

        # Если есть хотя бы что-то одно (очаги или предочаги) —
        # используем продвинутую карту через ReportGenerator.
        # Раньше при пустом raw_clusters всегда включался fallback на простую
        # карту — это приводило к тому, что для малых регионов (например,
        # Севастополь с 0 очагов и 8 предочагами) показывалась простая карта
        # без слоёв, попапов, линейки и т.д. ReportGenerator.generate_cluster_map
        # умеет работать с пустым списком clusters и непустыми preclusters.

        # Разделяем: текущие очаги + АППГ-повторённые + исчезнувшие (отдельно)
        # prev_matched — это очаги прошлого периода, которые повторились
        # в текущем. Они отображаются на карте отдельным слоем (светло-голубые
        # маркеры с пунктирной границей), чтобы пользователь видел, какие
        # именно АППГ-очаги «превратились» в текущие.
        current_only = [
            c for c in raw_clusters
            if not c.get("_is_lost", False) and not c.get("_is_prev_matched", False)
        ]
        lost_clusters = [c for c in raw_clusters if c.get("_is_lost", False)]
        prev_matched_clusters = [c for c in raw_clusters if c.get("_is_prev_matched", False)]

        # Предочаги — из отдельного поля task.raw_preclusters
        # (раньше брались из raw_clusters[0]["_preclusters"], что ломалось
        # при пустом списке очагов)
        preclusters = raw_preclusters

        # Камеры (если есть в кэше)
        cameras = []
        try:
            if camera_cache_module.has_cached_cameras(task.region_code):
                cameras = camera_cache_module.load_cameras_from_cache(
                    task.region_code
                ) or []
        except Exception as exc:
            logger.warning(f"Task {task.id}: camera load for map failed: {exc}")

        # Добавляем исчезнувшие и АППГ-повторённые очаги в основной список
        # с пометкой dynamics.status ('lost' или 'prev_matched'),
        # чтобы ReportGenerator отрисовал их как отдельные слои через dynamics.
        all_clusters_for_map = (
            list(current_only)
            + list(prev_matched_clusters)
            + list(lost_clusters)
        )

        # Генерируем карту через ReportGenerator
        gen = report_gen_module.ReportGenerator(
            region_name=task.region_name,
            period_label=task.period_label,
        )
        html = await asyncio.to_thread(
            gen.generate_cluster_map,
            all_clusters_for_map,
            preclusters if preclusters else None,
            cameras if cameras else None,
        )

        # Добавляем плашку про исчезнувшие очаги (если есть)
        if lost_clusters:
            lost_count = len(lost_clusters)
            lost_dtp = sum(c.get("total_accidents", 0) for c in lost_clusters)
            lost_deaths = sum(c.get("deaths", 0) for c in lost_clusters)
            banner = f"""
<div class="lost-banner" style="
  position:absolute;top:10px;right:10px;z-index:1000;
  background:#fff3e0;border:1px solid #ff9800;border-radius:6px;
  padding:8px 12px;font:12px/1.4 -apple-system,system-ui,sans-serif;
  box-shadow:0 2px 8px rgba(0,0,0,0.2);max-width:240px;">
  <b style="color:#d32f2f;">❌ Исчезнувшие очаги: {lost_count}</b><br>
  <span style="color:#555;">Это очаги, которые были в прошлом периоде,
  но не подтвердились в текущем.</span><br>
  <span style="color:#888;font-size:11px;">
  ДТП прошлого периода: {lost_dtp} | Погибло: {lost_deaths}</span>
</div>"""
            # Вставляем плашку после <body>
            html = html.replace(
                "<body>", f"<body>{banner}", 1
            ) if "<body>" in html else html + banner

        logger.info(
            f"Task {task.id}: clusters map generated — "
            f"{len(current_only)} текущих, "
            f"{len(prev_matched_clusters)} АППГ-повторённых, "
            f"{len(lost_clusters)} исчезнувших, "
            f"{len(preclusters)} предочагов, {len(cameras)} камер"
        )
        return html

    except Exception as exc:
        logger.exception(f"Task {task.id}: clusters map generation failed")
        return None


def _build_clusters_map_html(task: Task) -> str:
    """Простая Leaflet-карта очагов с маркерами и popup."""
    result = task.clusters_state.result
    if not result:
        return "<html><body>Нет данных</body></html>"

    clusters = result.get("clusters", [])
    preclusters = result.get("preclusters", [])

    # Разделяем: текущие / АППГ-повторённые / исчезнувшие
    # по dynamics.status. Каждая группа — отдельный слой на карте.
    current_clusters = []
    lost_clusters = []
    prev_matched_clusters = []
    for c in clusters:
        st = c.get("dynamics", {}).get("status")
        if st == "lost":
            lost_clusters.append(c)
        elif st == "prev_matched":
            prev_matched_clusters.append(c)
        else:
            current_clusters.append(c)

    # Leaflet-карта
    def _build_marker_js(c: dict, color: str, layer_var: str) -> str:
        center = c.get("center")
        if not center:
            return ""
        lat, lon = center["lat"], center["lon"]
        road = (c.get("road") or "Не указана").replace("'", "\\'")
        total = c.get("total_accidents", 0)
        deaths = c.get("deaths", 0)
        injured = c.get("injured", 0)
        zone = c.get("zone_type", "")
        popup_html = (
            f"<b>{road}</b><br>"
            f"ДТП: {total} | Погибло: {deaths} | Ранено: {injured}<br>"
            f"Тип: {zone}"
        ).replace('"', "&quot;")
        radius = max(8, min(30, total * 2))
        return (
            f"L.circleMarker([{lat}, {lon}], "
            f"{{radius: {radius}, color: '{color}', "
            f"fillColor: '{color}', fillOpacity: 0.6}})"
            f".addTo({layer_var}).bindPopup(\"{popup_html}\");"
        )

    markers_js = []
    # Слои: current — обычные цвета по тяжести, prev_matched — голубой,
    # lost — серый. Layer groups позволяют включать/выключать группы через UI Leaflet.
    markers_js.append("var currentLayer = L.layerGroup().addTo(map);")
    markers_js.append("var prevMatchedLayer = L.layerGroup().addTo(map);")
    markers_js.append("var lostLayer = L.layerGroup().addTo(map);")
    for c in current_clusters:
        markers_js.append(_build_marker_js(c, _color_for_severity(c), "currentLayer"))
    for c in prev_matched_clusters:
        # АППГ-повторённые — голубой (#5ac8fa), чтобы визуально отличить
        # от текущих и от исчезнувших (серый).
        markers_js.append(_build_marker_js(c, "#5ac8fa", "prevMatchedLayer"))
    for c in lost_clusters:
        # Исчезнувшие — светло-серый цвет, чтобы визуально отделить от активных очагов
        markers_js.append(_build_marker_js(c, "#c0c0c0", "lostLayer"))

    for p in preclusters:
        center = p.get("center")
        if not center:
            continue
        lat, lon = center["lat"], center["lon"]
        road = (p.get("road") or "Не указана").replace("'", "\\'")
        total = p.get("total_accidents", 0)
        popup_html = (
            f"<b>Предочаг:</b> {road}<br>"
            f"ДТП: {total}"
        ).replace('"', "&quot;")
        markers_js.append(
            f"L.circleMarker([{lat}, {lon}], "
            f"{{radius: 8, color: '#ff9500', "
            f"fillColor: '#ff9500', fillOpacity: 0.4, "
            f"dashArray: '4,4'}})"
            f".addTo(map).bindPopup(\"{popup_html}\");"
        )

    # Центр карты — первый очаг или дефолт
    if clusters and clusters[0].get("center"):
        center_lat = clusters[0]["center"]["lat"]
        center_lon = clusters[0]["center"]["lon"]
    else:
        center_lat, center_lon = 55.75, 37.62

    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Очаги ДТП — {task.region_name}</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
html, body, #map {{ margin: 0; padding: 0; height: 100%; width: 100%; }}
.legend {{
  position: absolute; bottom: 10px; left: 10px; z-index: 1000;
  background: white; padding: 8px 12px; border-radius: 6px;
  font: 12px/1.4 -apple-system, system-ui, sans-serif;
  box-shadow: 0 2px 8px rgba(0,0,0,0.2);
}}
.legend-item {{ display: flex; align-items: center; gap: 6px; margin: 2px 0; }}
.legend-dot {{ width: 12px; height: 12px; border-radius: 50%; }}
</style>
</head>
<body>
<div id="map"></div>
<div class="legend">
<div class="legend-item"><span class="legend-dot" style="background:#5ac8fa;border:2px dashed #007aff;"></span>АППГ (повторён в текущем)</div>
<div class="legend-item"><span class="legend-dot" style="background:#c0c0c0;border:2px dashed #9e9e9e;"></span>Исчезнувший очаг</div>
<div class="legend-item"><span class="legend-dot" style="background:#2481cc"></span>Очаг (низкая тяжесть)</div>
<div class="legend-item"><span class="legend-dot" style="background:#ff9500"></span>Очаг (высокая тяжесть)</div>
<div class="legend-item"><span class="legend-dot" style="background:#34c759;opacity:0.5"></span>Предочаг</div>
</div>
<script>
var map = L.map('map').setView([{center_lat}, {center_lon}], 11);
L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
  maxZoom: 18, attribution: '&copy; OpenStreetMap'
}}).addTo(map);
{chr(10).join(markers_js)}
L.control.layers({{}}, {{
  "Текущие очаги": currentLayer,
  "АППГ (повторённые)": prevMatchedLayer,
  "Исчезнувшие очаги": lostLayer
}}, {{collapsed: false}}).addTo(map);
</script>
</body>
</html>"""
    return html


def _color_for_severity(cluster: dict) -> str:
    """Цвет очага по тяжести."""
    deaths = cluster.get("deaths", 0)
    if deaths >= 3:
        return "#ff3b30"
    if deaths >= 1:
        return "#ff9500"
    return "#2481cc"


# ============================================================
# Excel-выгрузка: очаги (4 листа)
# ============================================================
async def generate_clusters_excel(task: Task) -> Optional[bytes]:
    """
    Генерирует Excel-файл с очагами ДТП (4 листа):
      Лист 1 «Очаги ДТП» — текущие очаги
      Лист 2 «Динамика очагов» — текущие + исчезнувшие со статусом
      Лист 3 «Детализация ДТП» — все ДТП по периодам
      Лист 4 «Предочаги» — места, не дотянувшие до очага

    Использует excel_generator.generate_concentration_dynamics_file() из бота.
    """
    if not task.raw_clusters and not task.raw_preclusters:
        # Нет ни очагов, ни предочагов — выгружать нечего
        return None

    try:
        conc_module = _import_module("concentration_points")
        excel_module = _import_module("excel_generator")

        raw_clusters = task.raw_clusters or []
        raw_preclusters = task.raw_preclusters or []
        # current_only — только текущие очаги (без lost и prev_matched)
        # для листа 1 «Очаги ДТП». Лист 2 «Динамика очагов» использует
        # raw_clusters целиком (текущие + prev_matched + lost).
        current_only = [
            c for c in raw_clusters
            if not c.get("_is_lost", False) and not c.get("_is_prev_matched", False)
        ]
        # Предочаги — из отдельного поля (раньше raw_clusters[0]["_preclusters"],
        # что ломалось при пустом списке очагов)
        preclusters = raw_preclusters

        # Лист 1: очаги текущего года
        current_data = conc_module.build_concentration_excel_data(current_only)
        current_columns = conc_module.get_concentration_column_names()

        # Лист 2: динамика (текущие + исчезнувшие)
        dynamics_data = conc_module.build_dynamics_excel_data(raw_clusters)
        dynamics_columns = conc_module.get_dynamics_column_names()

        # Лист 3: детализация ДТП
        detail_data = conc_module.build_dynamics_detail_data(
            raw_clusters,
            task.period_label,
            task.prev_label or "",
        )
        detail_columns = conc_module.get_dynamics_detail_column_names()

        # Лист 4: предочаги
        precluster_data = None
        precluster_columns = None
        if preclusters:
            precluster_data = conc_module.build_precluster_excel_data(preclusters)
            precluster_columns = conc_module.get_precluster_column_names()

        # Генерация Excel (тяжёлая операция — в потоке)
        xlsx_bytes = await asyncio.to_thread(
            excel_module.generate_concentration_dynamics_file,
            current_data, current_columns,
            dynamics_data, dynamics_columns,
            detail_data, detail_columns,
            precluster_data, precluster_columns,
        )

        logger.info(
            f"Task {task.id}: clusters Excel generated — "
            f"{len(current_data)} очагов, {len(dynamics_data)} в динамике, "
            f"{len(detail_data)} ДТП, {len(preclusters)} предочагов"
        )
        return xlsx_bytes

    except Exception as exc:
        logger.exception(f"Task {task.id}: clusters Excel generation failed")
        return None


# ============================================================
# Excel-выгрузка: статистика по точке (2 листа)
# ============================================================
async def generate_point_stats_excel(task: Task) -> Optional[bytes]:
    """
    Генерирует Excel-файл со статистикой по точке (2 листа):
      Лист 1 — текущий период (все ДТП в радиусе с детализацией)
      Лист 2 — прошлый период (если есть данные)

    Требует предварительно выполненный compute_point_stats —
    берёт сохранённые в task карточки (с дистанцией _dist_m).
    """
    if not task.last_point_cards_current and not task.last_point_cards_prev:
        return None

    try:
        point_stats_module = _import_module("point_statistics")
        excel_module = _import_module("excel_generator")

        current_rows, prev_rows = point_stats_module.build_point_stats_excel_data(
            task.last_point_cards_current,
            task.last_point_cards_prev if task.last_point_cards_prev else None,
            task.period_label,
            task.prev_label or "",
        )
        columns = point_stats_module.get_point_stats_column_names()

        xlsx_bytes = await asyncio.to_thread(
            excel_module.generate_point_stats_file,
            current_rows,
            prev_rows if prev_rows else None,
            columns,
            task.period_label,
            task.prev_label if prev_rows else None,
        )

        logger.info(
            f"Task {task.id}: point stats Excel generated — "
            f"{len(current_rows)} текущих, {len(prev_rows)} прошлых"
        )
        return xlsx_bytes

    except Exception as exc:
        logger.exception(f"Task {task.id}: point stats Excel generation failed")
        return None


# ============================================================
# Карта статистики по точке
# ============================================================
async def generate_point_stats_map_html(
    task: Task,
    lat: float,
    lon: float,
    radius_m: int,
) -> Optional[str]:
    """
    Генерирует HTML-карту статистики по точке через
    ReportGenerator.generate_point_stats_map() из бота.

    Карта: точка + радиус + ДТП (текущий/прошлый) + камеры в радиусе.
    """
    if not task.cards:
        return None

    try:
        report_gen_module = _import_module("report_generator")
        point_stats_module = _import_module("point_statistics")
        camera_cache_module = _import_module("camera_cache")
        camera_matcher_module = _import_module("camera_matcher")

        # Загружаем прошлый год (если ещё нет)
        if not task.prev_cards_loaded:
            await ensure_prev_cards(task)
        prev_cards = task.prev_cards or []

        # Камеры в радиусе
        cameras_in_radius = []
        try:
            if camera_cache_module.has_cached_cameras(task.region_code):
                all_cameras = camera_cache_module.load_cameras_from_cache(
                    task.region_code
                ) or []
                for cam in all_cameras:
                    d = camera_matcher_module.haversine(
                        lat, lon, cam["lat"], cam["lon"]
                    )
                    if d <= radius_m:
                        cameras_in_radius.append({**cam, "distance_m": round(d, 0)})
        except Exception as exc:
            logger.warning(
                f"Task {task.id}: cameras for point map failed: {exc}"
            )

        gen = report_gen_module.ReportGenerator(
            region_name=task.region_name,
            period_label=task.period_label,
        )
        html = await asyncio.to_thread(
            gen.generate_point_stats_map,
            lat, lon, radius_m,
            task.cards,
            prev_cards if prev_cards else None,
            cameras_in_radius if cameras_in_radius else None,
            task.period_label,
            task.prev_label or "",
        )

        logger.info(
            f"Task {task.id}: point stats map generated — "
            f"lat={lat}, lon={lon}, radius={radius_m}м, "
            f"{len(cameras_in_radius)} камер в радиусе"
        )
        return html

    except Exception as exc:
        logger.exception(f"Task {task.id}: point stats map generation failed")
        return None


# ============================================================
# AI-анализ
# ============================================================
async def start_llm_summary(task: Task, provider: str = "free") -> None:
    """
    Асинхронная генерация LLM-резюме.

    provider: "free" (ZhipuAI/GLM) или "paid" (DeepSeek).

    Внутри использует asyncio.wait_for с max duration (5 минут), чтобы
    при зависании LLM (бесконечные 5xx-ретраи, потеря соединения)
    операция гарантированно завершилась с понятной ошибкой, а не висела
    в статусе RUNNING вечно.
    """
    state = task.llm_summary_state
    state.status = AnalysisStatus.RUNNING
    state.progress = 5
    state.stage = "Подготовка данных..."
    state.started_at = datetime.now(timezone.utc)
    state.error = None
    state.result = None

    # Защита от зависания: максимум 5 минут на всю операцию.
    # Если LLM не ответил за 5 мин — что-то не так (сервис недоступен,
    # бесконечные ретраи,超大 промпт) — лучше упасть с понятной ошибкой.
    MAX_LLM_DURATION_SEC = 300

    try:
        # Запускаем реальную работу в task и ограничиваем по времени.
        # Используем shield, чтобы wait_for cancel не отменил сам task
        # (он продолжит работать в фоне, но результат уже не запишется).
        try:
            await asyncio.wait_for(
                _run_llm_summary_inner(task, provider, state),
                timeout=MAX_LLM_DURATION_SEC,
            )
        except asyncio.TimeoutError:
            elapsed = int(
                (datetime.now(timezone.utc) - state.started_at).total_seconds()
            )
            err_msg = (
                f"LLM-анализ превысил максимально допустимое время "
                f"({MAX_LLM_DURATION_SEC} сек, прошло {elapsed} сек). "
                f"Возможно, сервис нейросети перегружен или промпт слишком большой. "
                f"Попробуйте ещё раз через несколько минут или используйте "
                f"другой провайдер."
            )
            logger.error(
                f"Task {task.id}: LLM summary timeout after {elapsed}s"
            )
            state.status = AnalysisStatus.FAILED
            state.error = err_msg
            state.stage = "Превышено время ожидания"
            state.finished_at = datetime.now(timezone.utc)

    except Exception as exc:
        logger.exception(f"Task {task.id}: LLM summary failed")
        state.status = AnalysisStatus.FAILED
        state.error = str(exc)
        state.stage = "Ошибка"
        state.finished_at = datetime.now(timezone.utc)


async def _run_llm_summary_inner(
    task: Task, provider: str, state: AnalysisState,
) -> None:
    """Внутренняя логика LLM-саммари — вынесена, чтобы можно было
    обернуть в asyncio.wait_for для max duration."""
    config = _import_module("config")

    # Проверяем доступность провайдера
    if provider == "paid":
        if not (config.LLM_PAID_API_KEY and config.LLM_PAID_API_URL):
            raise RuntimeError(
                "Платный LLM-провайдер не настроен "
                "(LLM_PAID_API_KEY/LLM_PAID_API_URL)"
            )
    else:
        if not config.LLM_API_KEY:
            raise RuntimeError(
                "Бесплатный LLM-провайдер не настроен (LLM_API_KEY)"
            )

    state.progress = 10
    state.stage = "Загрузка данных за прошлый год..."
    if not task.prev_cards_loaded:
        await ensure_prev_cards(task)

    state.progress = 20
    state.stage = "Расчёт сравнительных метрик..."
    comp_result = await ensure_comparison(task)
    if not comp_result.get("ok"):
        raise RuntimeError(comp_result.get("error", "Не удалось рассчитать comparison"))
    comparison = comp_result["comparison"]

    state.progress = 35
    state.stage = "Расчёт очагов ДТП для контекста..."

    # Используем готовые очаги, если уже рассчитаны
    clusters_ctx = ""
    if task.clusters_state.status == AnalysisStatus.DONE and task.clusters_state.result:
        llm_module = _import_module("llm_analyzer")
        # Передаём ВСЕ очаги (а не только топ-10), чтобы format_clusters_for_prompt
        # могла разделить их по категориям (повторные/новые/исчезнувшие).
        # Раньше брали [:10] и LLM видел «солянку» из текущих и прошлых очагов.
        # Теперь метод сам сортирует и режет по max_clusters в каждой категории.
        fake_clusters = [
            {
                "road": c.get("road", ""),
                "zone_type": c.get("zone_type", ""),
                "total_accidents": c.get("total_accidents", 0),
                "deaths": c.get("deaths", 0),
                "injured": c.get("injured", 0),
                # None (смешанный тип) -> пустая строка для UI
                "dominant_type": c.get("dominant_type") or "",
                "type_counter": c.get("type_counter", {}),
                "start_pos": c.get("start_pos"),
                "end_pos": c.get("end_pos"),
                "dates": c.get("dates", []),
                # Передаём dynamics (status, prev_total, matched_prev_numbers, neighbors)
                # и флаги is_lost/is_prev_matched — по ним LLM поймёт, к какой
                # категории относится очаг.
                "dynamics": c.get("dynamics", {}),
                "_is_lost": c.get("is_lost", False),
                "_is_prev_matched": c.get("is_prev_matched", False),
            }
            for c in task.clusters_state.result.get("clusters", [])
        ]
        clusters_ctx = llm_module.format_clusters_for_prompt(
            fake_clusters, max_clusters=10,
        )

    state.progress = 50
    state.stage = "Формирование промпта..."

    llm_module = _import_module("llm_analyzer")
    analytics_module = _import_module("analytics")

    # Кросс-таблицы (только для бесплатного метода)
    cross_tables_ctx = ""
    if provider == "free":
        try:
            current_cross = analytics_module.calculate_cross_tables(task.cards)
            prev_cross = None
            if task.prev_cards:
                prev_cross = analytics_module.calculate_cross_tables(
                    task.prev_cards
                )
            cross_tables_ctx = llm_module.format_cross_tables_for_prompt(
                current_cross, prev_cross,
                task.period_label,
                task.prev_label or "",
            )
            # Этап 2: статистические метрики (severity rates, Z-score, χ²)
            stats = analytics_module.calculate_statistical_metrics(current_cross)
            stats_text = llm_module.format_statistical_metrics_for_prompt(stats)
            if stats_text and not stats_text.endswith("(недостаточно данных для статистического анализа)"):
                cross_tables_ctx += "\n\n" + stats_text
        except Exception as exc:
            logger.warning(f"Cross-tables failed: {exc}")

    state.progress = 60
    state.stage = (
        "Запрос к нейросети (15-60 сек)... "
        "Не закрывайте вкладку."
    )

    # Диагностическое логирование: размер промпта и кросс-таблиц.
    # После добавления 7 новых кросс-таблиц (БДД-факторы + профиль ТС)
    # промпт может вырасти до ~50k+ символов, что вызывает 500-е ошибки
    # у GLM-4.7-Flash. Логируем состав, чтобы видеть, какие таблицы
    # раздули промпт.
    logger.info(
        f"Task {task.id}: LLM prompt sizes — "
        f"clusters_ctx={len(clusters_ctx)} симв., "
        f"cross_tables_ctx={len(cross_tables_ctx)} симв., "
        f"provider={provider}"
    )

    # Вызываем LLM с уменьшенным числом ретраев (3 вместо 5) — для summary
    # долгие ретраи (7.5 мин) плохой UX, лучше быстро упасть и дать
    # пользователю кнопку «Повторить».
    summary = await llm_module.get_ai_summary(
        comparison=comparison,
        reg_name=task.region_name,
        current_label=task.period_label,
        prev_label=task.prev_label or "прошлый период",
        raw_supplement="",
        news_context="",
        clusters_context=clusters_ctx,
        cross_tables_context=cross_tables_ctx,
        provider=provider,
        current_cards=task.cards if provider == "paid" else None,
        prev_cards=task.prev_cards if provider == "paid" else None,
        max_retries=3,
    )

    state.result = {
        "text": summary,
        "provider": provider,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    state.status = AnalysisStatus.DONE
    state.progress = 100
    state.stage = "Готово"
    state.finished_at = datetime.now(timezone.utc)

    logger.info(f"Task {task.id}: LLM summary done ({provider})")


async def ask_llm_question(
    task: Task,
    question: str,
    provider: str = "free",
) -> Dict[str, Any]:
    """
    Синхронный (но длительный) ответ на вопрос пользователя.

    Не использует state-машину — просто вызывает LLM и возвращает ответ.
    """
    if not question or len(question.strip()) < 3:
        return {"ok": False, "error": "Слишком короткий вопрос"}

    try:
        config = _import_module("config")
        if provider == "paid":
            if not (config.LLM_PAID_API_KEY and config.LLM_PAID_API_URL):
                return {"ok": False, "error": "Платный LLM не настроен"}
        else:
            if not config.LLM_API_KEY:
                return {"ok": False, "error": "Бесплатный LLM не настроен"}

        # Гарантируем comparison
        comp_result = await ensure_comparison(task)
        if not comp_result.get("ok"):
            return {"ok": False, "error": comp_result.get("error")}
        comparison = comp_result["comparison"]

        llm_module = _import_module("llm_analyzer")
        analytics_module = _import_module("analytics")

        # Кросс-таблицы (только для бесплатного)
        cross_tables_ctx = ""
        if provider == "free":
            try:
                current_cross = analytics_module.calculate_cross_tables(task.cards)
                cross_tables_ctx = llm_module.format_cross_tables_for_prompt(
                    current_cross, None,
                    task.period_label,
                    task.prev_label or "",
                )
                # Этап 2: статистические метрики (severity rates, Z-score, χ²)
                stats = analytics_module.calculate_statistical_metrics(current_cross)
                stats_text = llm_module.format_statistical_metrics_for_prompt(stats)
                if stats_text and not stats_text.endswith("(недостаточно данных для статистического анализа)"):
                    cross_tables_ctx += "\n\n" + stats_text
            except Exception as exc:
                # Не валить весь Q&A, если кросс-таблицы упали —
                # LLM ответит на основе comparison + clusters_context.
                # Но залогировать нужно, иначе ошибка будет невидимой.
                logger.warning(
                    f"Task {task.id}: Q&A cross-tables failed: {exc}"
                )

        # Очаги (если есть)
        clusters_ctx = ""
        if task.clusters_state.status == AnalysisStatus.DONE and task.clusters_state.result:
            # Передаём ВСЕ очаги с dynamics — format_clusters_for_prompt
            # сама разделит по категориям (повторные/новые/исчезнувшие).
            fake_clusters = [
                {
                    "road": c.get("road", ""),
                    "zone_type": c.get("zone_type", ""),
                    "total_accidents": c.get("total_accidents", 0),
                    "deaths": c.get("deaths", 0),
                    "injured": c.get("injured", 0),
                    # None (смешанный тип) -> пустая строка для UI
                    "dominant_type": c.get("dominant_type") or "",
                    "type_counter": c.get("type_counter", {}),
                    "start_pos": c.get("start_pos"),
                    "end_pos": c.get("end_pos"),
                    "dates": c.get("dates", []),
                    "dynamics": c.get("dynamics", {}),
                    "_is_lost": c.get("is_lost", False),
                    "_is_prev_matched": c.get("is_prev_matched", False),
                }
                for c in task.clusters_state.result.get("clusters", [])
            ]
            clusters_ctx = llm_module.format_clusters_for_prompt(
                fake_clusters, max_clusters=10,
            )

        # Преобразуем сохранённую историю Q&A (для UI) в формат OpenAI
        # и передаём в LLM — чтобы модель понимала follow-up-вопросы.
        # Берём последние 12 сообщений (6 пар Q&A), чтобы не раздувать промпт.
        history_for_llm: list[dict[str, str]] = []
        for h in task.llm_qa_history:
            q = h.get("question", "")
            a = h.get("answer", "")
            if q:
                history_for_llm.append({"role": "user", "content": q})
            if a:
                history_for_llm.append({"role": "assistant", "content": a})
        if len(history_for_llm) > 12:
            history_for_llm = history_for_llm[-12:]

        # Диагностическое логирование: видно, доходит ли история до LLM
        hist_total_chars = sum(len(m.get("content", "")) for m in history_for_llm)
        logger.info(
            f"Task {task.id}: LLM ask — "
            f"qa_history={len(task.llm_qa_history)} records, "
            f"history_for_llm={len(history_for_llm)} msgs, "
            f"history_chars={hist_total_chars}, "
            f"provider={provider}"
        )

        answer = await llm_module.get_ai_answer(
            question=question,
            comparison=comparison,
            reg_name=task.region_name,
            current_label=task.period_label,
            prev_label=task.prev_label or "прошлый период",
            raw_supplement="",
            news_context="",
            clusters_context=clusters_ctx,
            cross_tables_context=cross_tables_ctx,
            provider=provider,
            history=history_for_llm,
        )

        # Сохраняем в историю
        task.llm_qa_history.append({
            "question": question,
            "answer": answer,
            "provider": provider,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        # Ограничиваем историю 10 записями
        if len(task.llm_qa_history) > 10:
            task.llm_qa_history = task.llm_qa_history[-10:]

        return {"ok": True, "answer": answer, "provider": provider}

    except Exception as exc:
        logger.exception(f"Task {task.id}: LLM ask failed")
        return {"ok": False, "error": str(exc)}


def get_llm_providers_status() -> Dict[str, bool]:
    """Возвращает статус доступности LLM-провайдеров."""
    try:
        config = _import_module("config")
        return {
            "free": bool(config.LLM_API_KEY),
            "paid": bool(
                getattr(config, "LLM_PAID_API_KEY", None)
                and getattr(config, "LLM_PAID_API_URL", None)
            ),
            "free_model": getattr(config, "LLM_MODEL", "glm-4-flash"),
            "paid_model": getattr(config, "LLM_PAID_MODEL", "deepseek-chat"),
        }
    except Exception:
        return {"free": False, "paid": False,
                "free_model": "", "paid_model": ""}


# ============================================================
# Очистка старых задач (для периодического вызова)
# ============================================================
async def cleanup_old_tasks(max_age_hours: int = 24) -> int:
    """
    Удаляет задачи старше max_age_hours.

    Удаляет из:
    - In-memory _tasks и тяжёлого state (db/repository._TASKS_HEAVY_STATE)
    - БД (если доступна) — через repository.delete_old_tasks
    - Диска — data/tasks/{task_id}/

    Возвращает количество удалённых задач.
    """
    # 1. Через repository (БД + memory + диск)
    deleted = 0
    try:
        from ..db.repository import delete_old_tasks, drop_heavy_state
        deleted = await delete_old_tasks(max_age_hours, _PROJECT_ROOT)
    except Exception as exc:
        logger.warning(f"cleanup_old_tasks: repository call failed: {exc}")

    # 2. In-memory _tasks очистка (дублирует, но безопасно — на случай
    # если repository не подхватил что-то, или БД недоступна и fallback
    # в repository отработал не полностью)
    now = datetime.now(timezone.utc)
    cutoff = now.timestamp() - max_age_hours * 3600
    to_delete = [
        tid for tid, task in _tasks.items()
        if task.created_at.timestamp() < cutoff
    ]
    for tid in to_delete:
        task = _tasks.pop(tid, None)
        if task:
            for f in task.files:
                try:
                    Path(f["path"]).unlink(missing_ok=True)
                except Exception:
                    pass
            try:
                task_dir = _PROJECT_ROOT / "data" / "tasks" / tid
                if task_dir.exists():
                    task_dir.rmdir()
            except Exception:
                pass

    if to_delete or deleted:
        logger.info(
            f"Cleaned up: in-memory={len(to_delete)}, total(inc. DB)={deleted}"
        )
    return max(deleted, len(to_delete))
