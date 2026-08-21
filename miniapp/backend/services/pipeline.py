"""
Пайплайн выполнения задачи выгрузки: create → execute → DONE.

Содержит:
- create_task() — создание и регистрация новой задачи
- execute_task() / _execute_task_impl() — основной пайплайн:
  FETCHING → ANALYTICS → DONE
- ensure_prev_cards() — lazy загрузка карточек прошлого года (АППГ)
- _task_dir() — хелпер

Использует Semaphore (из config: max_concurrent_tasks, default=3) для ограничения одновременных выгрузок к API ГИБДД.

Excel-файлы больше НЕ генерируются в пайплайне. Пользователь может:
1. Скачать их по кнопкам из вкладки «Файлы» (ленивая генерация).
2. Воспользоваться отдельной вкладкой «Выгрузка файлов».

Это экономит 5-8 секунд на каждой выгрузке — пользователь сразу
видит карту, аналитику и очаги без ожидания генерации Excel.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import _imports
from .models import Task, TaskStatus
from .task_registry import (
    _gen_task_id,
    _now_utc,
    _register_task,
    _task_factory,  # noqa: F401 — re-exported для тестов
    get_task_async,
)

logger = logging.getLogger(__name__)


# ============================================================
# Sprint 7 / Фаза C.2.4: feature flag для core/ path
# ============================================================
def _should_use_core_path() -> bool:
    """Возвращает True если GIBDD_USE_CORE_PIPELINE=1 (pipeline routing через core/).

    По умолчанию False — backward compatibility с production-деплоями,
    не знающими про C.2.4.

    Когда True, синхронные CPU-bound шаги (ANALYTICS)
    в _execute_task_impl идут через miniapp.backend.core.* sync-функции
    через asyncio.to_thread().
    """
    return os.environ.get("GIBDD_USE_CORE_PIPELINE", "0") == "1"


def _core_path_status() -> str:
    """Возвращает 'core' или 'legacy' — для логирования в начале execute_task."""
    return "core" if _should_use_core_path() else "legacy"


# === Фаза 1.1: Semaphore на одновременные выгрузки ===
# Ограничивает количество параллельно выполняемых execute_task().
# Читается из config.py (env MAX_CONCURRENT_TASKS).
# Рекомендации: 3 для 2-10 юзеров, 5 для 10-30, 8 для 30+.
# При 5+ одновременных запросах API ГИБДД может возвращать 429/502.
try:
    from ..config import settings
    MAX_CONCURRENT_TASKS: int = settings.max_concurrent_tasks
except Exception:
    MAX_CONCURRENT_TASKS: int = 3
_EXECUTE_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_TASKS)


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
    - В in-memory _tasks (с LRU-eviction, см. _register_task)
    - В БД через repository.save_task (если DATABASE_URL задан)

    Hotfix Sprint 7: раньше save_task запускался через fire-and-forget
    asyncio.create_task(save_task(task)) без done-callback — если корутина
    падала или не успевала выполниться до рестарта контейнера, задача
    терялась навсегда. Теперь:
    - _register_task() гарантирует задачу в task_registry._tasks (in-memory)
    - asyncio.shield(save_task()) защищает persist при shutdown
    - done-callback логирует ошибки save_task
    - Дублирующий await save_task(task) в router dtp.py:create_dtp_task
      гарантирует persist ДО запуска execute_task.
    """
    task_id = _gen_task_id()
    task = Task(
        id=task_id,
        user_id=user_id,
        region_code=region_code,
        region_name=region_name,
        period_label=period_label,
        dat_list=dat_list,
        raw_query=raw_query,
    )
    _register_task(task)

    # Task уже в task_registry._tasks через _register_task() выше —
    # отдельная запись в repository._TASKS_MEMORY не нужна (удалено
    # при консолидации кэшей: _tasks — единственный in-memory источник).

    # Sprint 7 / Phase C.3: сохраняем начальный snapshot в Redis.
    # Это закрывает окно между dispatch (отправкой в Celery) и моментом,
    # когда воркер вызовет _init_snapshot. Без этого в первые секунды
    # после dispatch в Redis нет snapshot, и _maybe_merge_redis_snapshot
    # возвращает None — фронтенд видит 0%.
    # save_task_state ожидает ПОЛНЫЙ Task-объект (с user_id, region_code, ...)
    # — у нас как раз такой.
    try:
        from worker.task_state import save_task_state
        if save_task_state(task):
            logger.debug(f"create_task({task_id}): initial snapshot saved to Redis")
    except Exception as exc:
        # Не роняем создание задачи — fallback на in-memory + воркер сам
        # создаст snapshot через _init_snapshot.
        logger.debug(f"create_task({task_id}): Redis snapshot save skipped: {exc}")

    # Асинхронно сохраняем в БД (если доступна).
    # Shield защищает корутину от отмены при shutdown — задача будет
    # сохранена даже если контейнер рестартует сразу после создания.
    try:
        from ..db.repository import save_task
        fut = asyncio.create_task(asyncio.shield(save_task(task)))
        fut.add_done_callback(_make_save_task_callback(task_id))
    except Exception as exc:
        logger.debug(f"create_task: DB save skipped: {exc}")

    return task


def _make_save_task_callback(task_id: str):
    """Возвращает done-callback для asyncio.create_task(save_task(...)).

    Логирует ошибки save_task, которые раньше терялись без trace.
    """
    def _callback(fut):
        try:
            fut.result()  # re-raise если было исключение
        except asyncio.CancelledError:
            # Normal at shutdown — корутина отменена при закрытии event loop
            logger.debug(
                f"create_task: save_task({task_id}) cancelled at shutdown"
            )
        except Exception as exc:
            logger.warning(
                f"create_task: save_task({task_id}) failed: {exc} — "
                f"задача доступна in-memory, но может быть потеряна при "
                f"рестарте (используйте shutdown-hook в main.py)"
            )
    return _callback


def _task_dir(task_id: str) -> Path:
    """Директория для файлов задачи (в data/tasks/)."""
    d = _imports._PROJECT_ROOT / "data" / "tasks" / task_id
    d.mkdir(parents=True, exist_ok=True)
    return d


async def execute_task(task_id: str) -> None:
    """
    Асинхронное выполнение задачи выгрузки.

    Шаги:
    1. FETCHING — выгрузка карточек ДТП через bot._fetch_cards_for_period
       (внутри: API → web-fallback → кэш)
    2. ANALYTICS — расчёт метрик через analytics.calculate_metrics

    Excel-файлы и HTML-карта НЕ генерируются в пайплайне.
    Они доступны по запросу через ленивую генерацию.

    На каждом переходе статуса — сохранение в БД через repository.save_task
    (если DATABASE_URL задан; иначе работает только in-memory).

    === Фаза 1.1: Semaphore на одновременные выгрузки ===
    Без ограничения 10 одновременных пользователей запустят 10 параллельных
    пайплайнов, каждый делает 12 HTTP-запросов к API ГИБДД → 120 запросов
    с одного IP → 429/502 блокировки. С Semaphore(3) — максимум 3
    одновременных выгрузки, остальные ждут в очереди (пользователь видит
    прогресс через polling статуса = FETCHING).
    """
    # Таймаут 600 сек (10 мин) — если задача зависла, отпускаем semaphore.
    # Обычно выгрузка занимает 30-60 сек, 10 мин — щедрый запас.
    try:
        async with _EXECUTE_SEMAPHORE:
            # === Фаза 1.6: Prometheus metrics ===
            from ..middleware.metrics import task_started, task_finished
            task_started()
            # Sprint 7 / Фаза C.2.4: логируем какой путь активен
            # (для мониторинга и дебага routing-решений).
            logger.info(
                f"Task {task_id}: execute_task started (path={_core_path_status()})"
            )
            try:
                await _execute_task_impl(task_id)
            finally:
                task_finished()
    except Exception as exc:
        logger.exception(f"Task {task_id} failed (semaphore-wrapped)")
        from .task_registry import _tasks
        task = _tasks.get(task_id)
        if task:
            task.status = TaskStatus.FAILED
            task.error = str(exc)
            task.updated_at = _now_utc()
            # Метрика: задача упала
            from ..middleware.metrics import record_task_status
            record_task_status("failed")
            # Hotfix Sprint 7: persist FAILED-статуса в БД. Раньше при
            # исключении в самом начале execute_task (до входа в
            # _execute_task_impl) задача оставалась в памяти со статусом
            # FAILED, но не сохранялась в БД. При рестарте — потеря.
            try:
                from ..db.repository import save_task
                await save_task(task)
            except Exception as persist_exc:
                logger.warning(
                    f"Task {task_id}: persist FAILED status failed: "
                    f"{persist_exc}"
                )


async def _execute_task_impl(task_id: str) -> None:
    """Реализация execute_task (вызывается под Semaphore)."""
    from .task_registry import _tasks

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
        task.updated_at = _now_utc()
        await _persist()

        bot_module = _imports._import_module("bot")

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
            task.updated_at = _now_utc()
            await _persist()
            return

        # Сводная статистика для отображения
        task.total_dtp = len(cards)
        task.total_dead = sum(int(c.get("pog", 0) or 0) for c in cards)
        task.total_injured = sum(int(c.get("ran", 0) or 0) for c in cards)

        # Сохраняем сырые карточки для последующего анализа
        # (очаги, статистика по точке, LLM-анализ, ленивая генерация Excel)
        task.cards = cards

        # === 2. ANALYTICS ===
        task.status = TaskStatus.ANALYTICS
        task.progress = 65
        task.updated_at = _now_utc()
        await _persist()

        try:
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

            # Sprint 7 / Фаза C.2.4: опциональный routing через core/
            # build_analytics_sync — pure sync функция, вызывается через
            # asyncio.to_thread чтобы не блокировать event loop.
            # В legacy path analytics_module.build_full_analytics вызывается
            # синхронно (CPU-bound но быстро — 200-500ms для 3000 cards).
            if _should_use_core_path():
                from ..core import build_analytics_sync

                task.analytics = await asyncio.to_thread(
                    build_analytics_sync,
                    cards,
                    prev_cards if prev_cards else None,
                    prev_label,
                )
                logger.info(f"Task {task_id}: ANALYTICS via core/build_analytics_sync")
            else:
                analytics_module = _imports._import_module("analytics")
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

        # === DONE ===
        task.status = TaskStatus.DONE
        task.progress = 100
        task.updated_at = _now_utc()
        await _persist()

        # === Фаза 1.6: Prometheus metric — задача завершена успешно ===
        try:
            from ..middleware.metrics import record_task_status
            record_task_status("done")
        except Exception:
            pass

        logger.info(
            f"Task {task_id} done: {task.total_dtp} ДТП, "
            f"{task.total_dead} погибших, {task.total_injured} раненых"
        )

    except Exception as exc:
        logger.exception(f"Task {task_id} failed")
        task.status = TaskStatus.FAILED
        task.error = str(exc)
        task.updated_at = _now_utc()
        await _persist()

        # === Фаза 1.6: Prometheus metric — задача упала ===
        try:
            from ..middleware.metrics import record_task_status
            record_task_status("failed")
        except Exception:
            pass


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
        bot_module = _imports._import_module("bot")
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
# Sprint 3.1: Восстановление task.cards из cards_cache
# ============================================================
async def ensure_cards(task: Task) -> Dict[str, Any]:
    """
    Гарантирует, что task.cards загружены.

    Проблема, которую решает эта функция:
      После рестарта контейнера или LRU eviction задачи из _tasks,
      тяжёлые поля (cards, prev_cards) теряются. attach_heavy_state()
      читает из _TASKS_HEAVY_STATE, но это in-memory кэш — после
      рестарта он пуст. cards_cache (PostgreSQL) при этом может быть
      жив (TTL=7 дней), но никто его не читает для восстановления
      task.cards при get_task_async().

      Результат: пользователь открывает старую задачу → task.cards=[]
      → ensure_comparison падает с "Карточки текущего периода не загружены"
      → LLM summary / clusters / point stats не работают.

    Решение:
      Вызывать _fetch_cards_for_period (он сам идёт в cards_cache → HIT,
      или скачивает заново → PUT в кэш). Это дешёвая операция при cache hit
      (<50 мс), и она восстанавливает task.cards до рабочего состояния.

    Возвращает:
        {"ok": True, "cards": [...]}
        или {"ok": False, "error": "..."}
    """
    # Быстрый путь: cards уже есть
    if task.cards:
        return {"ok": True, "cards": task.cards}

    # Если задача ещё в статусе FETCHING/ANALYTICS — не вмешиваемся,
    # pipeline.execute_task сам заполнит task.cards. Иначе можем перезаписать
    # данные в процессе их загрузки.
    if task.status in (TaskStatus.FETCHING, TaskStatus.ANALYTICS):
        return {
            "ok": False,
            "error": (
                f"Задача ещё выполняется (статус={task.status.value}), "
                f"карточки загружаются. Попробуйте через несколько секунд."
            ),
        }

    # Если задача упала на этапе выгрузки — нет смысла пытаться снова
    # (cards всё равно не скачаются). Возвращаем понятную ошибку.
    if task.status == TaskStatus.FAILED:
        return {
            "ok": False,
            "error": (
                f"Задача завершилась с ошибкой: {task.error or 'неизвестная'}. "
                f"Создайте новую задачу для этого региона."
            ),
        }

    # Восстанавливаем cards из cards_cache (или скачиваем заново)
    try:
        bot_module = _imports._import_module("bot")
        cards, errors = await bot_module._fetch_cards_for_period(
            dat_list=task.dat_list,
            reg_code=task.region_code,
            log_prefix=f"MiniApp[{task.id}]/restore",
            cache_result=True,
        )

        if not cards:
            return {
                "ok": False,
                "error": (
                    "Не удалось восстановить карточки ДТП. "
                    f"Ошибки: {'; '.join(errors[:3]) if errors else 'нет данных'}. "
                    "Создайте новую задачу для этого региона."
                ),
            }

        # Восстанавливаем сводные поля (могут быть пустыми после load_task)
        if not task.total_dtp:
            task.total_dtp = len(cards)
            task.total_dead = sum(int(c.get("pog", 0) or 0) for c in cards)
            task.total_injured = sum(int(c.get("ran", 0) or 0) for c in cards)

        task.cards = cards

        logger.info(
            f"Task {task.id}: cards restored from cache/API — "
            f"{len(cards)} ДТП, region={task.region_code}"
        )

        # Сбрасываем in-memory кэш analytics-расчётов, т.к. id(cards)
        # изменился. Иначе ensure_comparison может думать, что кэш валиден
        # (сравнивая id(task.cards) с cross_tables_cards_id), но cards
        # теперь другой объект.
        task.cross_tables = None
        task.cross_tables_cards_id = None
        task.current_metrics = None
        task.current_metrics_cards_id = None
        task.comparison = None  # пересчитать comparison с новыми cards

        return {"ok": True, "cards": task.cards}

    except Exception as exc:
        logger.exception(f"Task {task.id}: ensure_cards failed")
        return {"ok": False, "error": str(exc)}
