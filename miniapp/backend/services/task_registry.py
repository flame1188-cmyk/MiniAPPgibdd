"""
In-memory хранилище задач MiniApp (LRU + lock).

Хранит последние MAX_INMEMORY_TASKS задач в OrderedDict. При превышении
лимита вытесняет самую старую (с persistence в БД через repository.save_task).

Это центральный модуль: task_registry импортируется pipeline, cleanup,
facade'ом gibdd_service и тестами. Внешний код получает доступ к _tasks
через facade `gibdd_service._tasks` (для обратной совместимости).
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from threading import Lock
from typing import List, Optional

from . import _imports
from .models import Task

logger = logging.getLogger(__name__)


# In-memory хранилище задач (для production заменить на Redis/PostgreSQL)
#
# === Фаза 1.4: LRU-политика на _tasks ===
# Раньше это был обычный Dict[str, Task], который рос без ограничений.
# Каждая задача держит в памяти 3-12 MB (cards + prev_cards + raw_clusters +
# analytics). При 30 пользователях × 5 задач = 150 × 8 MB = ~1.2 GB —
# риск OOM на bothost с 2 GB RAM.
#
# OrderedDict + ручной LRU eviction: при добавлении новой задачи, если
# размер превышает MAX_INMEMORY_TASKS, вытесняем самую старую (FIFO по
# created_at). Тяжёлые поля вытесненной задачи сохраняются в БД (через
# repository.save_task), лёгкие метаданные остаются доступны через
# get_task_async() (lazy load из БД).
#
# MAX_INMEMORY_TASKS=50 выбрано как баланс: ~400 MB максимум в RAM,
# достаточно для одновременной работы 10-15 пользователей.
MAX_INMEMORY_TASKS = 50
_tasks: "OrderedDict[str, Task]" = OrderedDict()
_tasks_lock = Lock()


def _register_task(task: Task) -> None:
    """Добавляет задачу в _tasks с LRU-eviction.

    Если превышен лимит MAX_INMEMORY_TASKS — вытесняет самую старую задачу
    (по created_at). Вытесняемая задача предварительно сохраняется в БД
    (fire-and-forget через asyncio.create_task), чтобы метаданные не
    потерялись и были доступны через get_task_async().
    """
    with _tasks_lock:
        # Если задача уже есть — обновляем позицию (move_to_end)
        if task.id in _tasks:
            _tasks.move_to_end(task.id)
            _tasks[task.id] = task
            return

        # Вытесняем самые старые, если превышен лимит.
        # ВНИМАНИЕ: читаем лимит через lazy-import фасада gibdd_service, а не
        # через локальный binding MAX_INMEMORY_TASKS — иначе monkeypatch на
        # gibdd_service.MAX_INMEMORY_TASKS в тестах не сработает (патч на
        # фасаде не доходит до локальной копии в task_registry). Аналогично
        # паттерну с _imports._import_module.
        try:
            from . import gibdd_service as _facade
            _limit = getattr(_facade, "MAX_INMEMORY_TASKS", MAX_INMEMORY_TASKS)
        except Exception:
            _limit = MAX_INMEMORY_TASKS
        while len(_tasks) >= _limit:
            evicted_id, evicted_task = _tasks.popitem(last=False)
            logger.info(
                f"_tasks LRU: вытеснена задача {evicted_id} "
                f"(регион={evicted_task.region_code}, "
                f"возраст={evicted_task.created_at.isoformat()}) — "
                f"данные сохранены в БД, доступны через get_task_async()"
            )
            # Fire-and-forget persist в БД (если БД недоступна — теряем,
            # но это acceptable: задача старая, пользователь вряд ли
            # вернётся к ней в течение 24 часов)
            try:
                from ..db.repository import save_task
                asyncio.create_task(save_task(evicted_task))
            except Exception as exc:
                logger.debug(
                    f"_register_task: persist evicted {evicted_id} failed: {exc}"
                )

        _tasks[task.id] = task

    # === Фаза 1.6: обновляем Prometheus gauge размера _tasks ===
    try:
        from ..middleware.metrics import update_tasks_in_memory
        update_tasks_in_memory(len(_tasks))
    except Exception:
        pass


async def get_task_async(task_id: str) -> Optional[Task]:
    """Асинхронная версия get_task — проверяет и БД, и in-memory.

    Sprint 7 / Phase C.3: при USE_CELERY=true дополнительно мерджит snapshot
    task_state из Redis (если есть свежее) в Task. Это нужно, потому что в
    multi-режиме Celery worker пишет прогресс в Redis (через _update_snapshot
    в gibdd_tasks.execute_pipeline_task), а FastAPI процесс ничего не знает
    об этом прогрессе — у него своя копия Task в in-memory _tasks со старым
    status=PENDING. Без мерджа фронтенд будет вечно видеть PENDING, хотя
    Celery уже выполнил задачу.

    Логика мерджа:
      - Если Redis недоступен или snapshot'а нет → возвращаем Task как есть
      - Если snapshot.updated_at новее task.updated_at → применяем поля
        (status, progress, error, files, analytics, total_dtp/dead/injured)
      - Иначе → Task актуальнее (in-memory путь уже выполнился), не трогаем
    """
    # Сначала in-memory (быстро + есть тяжёлые поля)
    if task_id in _tasks:
        # LRU: обновляем позицию как "недавно использованную"
        with _tasks_lock:
            if task_id in _tasks:
                _tasks.move_to_end(task_id)
        task = _tasks[task_id]
        # Sprint 6: даже если задача in-memory, LLM-сессия могла быть
        # утеряна (например, task создан заново после рестарта, а
        # llm_sessions в БД осталась). Пробуем восстановить, если
        # оба поля пустые — это no-op если уже заполнено.
        await _try_restore_llm_session(task)
        # Sprint 7 / Phase C.3: мерджим Redis snapshot (если Celery работает)
        _maybe_merge_redis_snapshot(task)
        return task

    # Потом БД (если есть)
    try:
        from ..db.connection import is_db_ready
        from ..db.repository import load_task, attach_heavy_state
        if not is_db_ready():
            return None
        task = await load_task(task_id, _task_factory)
        if task is not None:
            attach_heavy_state(task)
            _register_task(task)  # добавляем в LRU-кэш
            # Sprint 6: восстанавливаем llm_summary_state + llm_qa_history
            # из llm_sessions (если они не были восстановлены через
            # attach_heavy_state из _TASKS_HEAVY_STATE).
            await _try_restore_llm_session(task)
            # Sprint 7 / Phase C.3: мерджим Redis snapshot
            _maybe_merge_redis_snapshot(task)
        return task
    except Exception as exc:
        logger.debug(f"get_task_async: DB load failed: {exc}")
        return _tasks.get(task_id)


def _maybe_merge_redis_snapshot(task: Task) -> None:
    """Sprint 7 / Phase C.3: мерджит Redis snapshot в Task (если Celery включён).

    Если USE_CELERY=true И REDIS_URL задан И в Redis есть snapshot для task_id
    новее, чем task.updated_at — применяет поля snapshot к task.

    Side effects:
        - Мутирует task (status, progress, error, files, analytics, total_*)
        - НЕ трогает тяжёлые поля (cards, prev_cards, raw_clusters) — они
          восстанавливаются через cards_cache/clusters_cache lazy.
        - НЕ пишет обратно в _tasks (это и так тот же объект)
    """
    try:
        from worker.task_state import load_task_state, snapshot_to_task_updates
    except Exception:
        # worker-модуль может быть не установлен в dev/тестах
        return

    try:
        snapshot = load_task_state(task.id)
    except Exception as exc:
        logger.debug(f"_maybe_merge_redis_snapshot({task.id}): load failed: {exc}")
        return

    if not snapshot or not isinstance(snapshot, dict):
        # Snapshot в Redis нет — это нормально в первые секунды после dispatch,
        # пока воркер не вызвал _init_snapshot. Логируем на INFO для диагностики.
        logger.info(
            f"_maybe_merge_redis_snapshot({task.id}): no snapshot in Redis "
            f"(task.status={task.status}, task.progress={task.progress}, "
            f"task.updated_at={task.updated_at})"
        )
        return

    # Проверяем свежесть: snapshot.updated_at должен быть новее task.updated_at
    snap_updated_str = snapshot.get("updated_at")
    if not snap_updated_str:
        logger.info(
            f"_maybe_merge_redis_snapshot({task.id}): snapshot has no updated_at "
            f"(snapshot keys: {sorted(snapshot.keys())})"
        )
        return
    try:
        from datetime import datetime
        snap_updated = datetime.fromisoformat(str(snap_updated_str).replace("Z", "+00:00"))
    except Exception as exc:
        logger.info(
            f"_maybe_merge_redis_snapshot({task.id}): cannot parse updated_at "
            f"'{snap_updated_str}': {exc}"
        )
        return

    # Логируем сравнение timestamps для диагностики
    task_updated_str = task.updated_at.isoformat() if task.updated_at else "None"
    snap_status = snapshot.get("status", "?")
    snap_progress = snapshot.get("progress", "?")
    logger.info(
        f"_maybe_merge_redis_snapshot({task.id}): "
        f"snap(updated={snap_updated_str}, status={snap_status}, progress={snap_progress}) "
        f"vs task(updated={task_updated_str}, status={task.status}, progress={task.progress})"
    )

    # Ослаблено с <= на <: если timestamps равны (миллисекунды совпали) —
    # всё равно мерджим, потому что snapshot от воркера содержит актуальные
    # поля (status, progress, files), а task.updated_at мог быть обновлён
    # в in-memory без реального изменения статуса.
    if task.updated_at and snap_updated < task.updated_at:
        # Task in-memory строго новее — не трогаем (in-memory path уже
        # выполнился, либо Celery snapshot устарел)
        logger.info(
            f"_maybe_merge_redis_snapshot({task.id}): SKIP — task.updated_at "
            f"({task_updated_str}) строго новее snap.updated_at ({snap_updated_str})"
        )
        return

    # Применяем поля snapshot к task
    updates = snapshot_to_task_updates(snapshot)
    if not updates:
        logger.info(f"_maybe_merge_redis_snapshot({task.id}): no updates to apply")
        return

    # Status — преобразуем строку в TaskStatus Enum
    new_status = updates.pop("status", None)
    if new_status:
        try:
            from .models import TaskStatus
            # Snapshot хранит строку ("pending", "fetching", "done", ...)
            # TaskStatus Enum может использовать uppercase или lowercase —
            # пробуем оба варианта.
            status_str = str(new_status).lower()
            for s in TaskStatus:
                if s.value.lower() == status_str:
                    task.status = s
                    break
        except Exception as exc:
            logger.debug(f"_maybe_merge_redis_snapshot({task.id}): status convert failed: {exc}")

    # Простые поля
    for field in ("progress", "error", "files", "analytics",
                  "total_dtp", "total_dead", "total_injured", "updated_at"):
        if field in updates:
            try:
                setattr(task, field, updates[field])
            except Exception:
                pass

    # AnalysisState поля (llm_summary_state, clusters_state)
    for state_field in ("llm_summary_state", "clusters_state"):
        if state_field in updates and updates[state_field]:
            state_dict = updates[state_field]
            try:
                from .models import AnalysisState, AnalysisStatus
                state = getattr(task, state_field, None)
                if state is None:
                    state = AnalysisState()
                    setattr(task, state_field, state)
                # Status
                state_status = state_dict.get("status")
                if state_status:
                    try:
                        state.status = AnalysisStatus(str(state_status).lower())
                    except Exception:
                        pass
                if "progress" in state_dict:
                    state.progress = state_dict["progress"]
                if "stage" in state_dict:
                    state.stage = state_dict["stage"]
                if "result" in state_dict:
                    state.result = state_dict["result"]
                if "error" in state_dict:
                    state.error = state_dict["error"]
                if state_dict.get("started_at"):
                    state.started_at = state_dict["started_at"]
                if state_dict.get("finished_at"):
                    state.finished_at = state_dict["finished_at"]
            except Exception as exc:
                logger.debug(
                    f"_maybe_merge_redis_snapshot({task.id}): "
                    f"{state_field} merge failed: {exc}"
                )

    logger.info(
        f"_maybe_merge_redis_snapshot({task.id}): MERGED "
        f"status={task.status} progress={task.progress} "
        f"files={len(task.files) if task.files else 0}"
    )


async def _try_restore_llm_session(task: Task) -> None:
    """
    Sprint 6: восстанавливает llm_summary_state и llm_qa_history из БД.

    Логика:
      - Если task.llm_qa_history пустой И task.llm_summary_state.status != DONE
        → загружаем из llm_sessions.
      - Если хотя бы одно заполнено (in-memory или из _TASKS_HEAVY_STATE)
        → ничего не делаем (не затираем актуальное состояние).

    Это гарантирует, что после рестарта приложения пользователь
    увидит резюме и Q&A-историю, не перегенерируя их.
    """
    # Быстрая проверка — нужно ли вообще что-то делать.
    has_summary = (
        task.llm_summary_state
        and task.llm_summary_state.status is not None
        and task.llm_summary_state.status.value == "done"
        and bool(task.llm_summary_state.result)
    )
    has_qa = bool(task.llm_qa_history)
    if has_summary and has_qa:
        return  # оба поля уже заполнены — ничего не делаем

    try:
        from ..db.repository import load_llm_session
        session = await load_llm_session(task.id)
        if session is None:
            return  # записи в БД нет — пользователь ещё не пользовался LLM

        # Восстанавливаем summary, если он пустой
        if not has_summary and session.get("summary_text"):
            try:
                from .gibdd_service import AnalysisStatus
                state = task.llm_summary_state
                state.status = AnalysisStatus.DONE
                state.progress = 100
                state.stage = "Готово (восстановлено из БД)"
                state.result = {
                    "text": session["summary_text"],
                    "provider": session.get("summary_provider") or "free",
                    "generated_at": (
                        session.get("summary_generated_at").isoformat()
                        if hasattr(session.get("summary_generated_at"), "isoformat")
                        else (session.get("summary_generated_at") or "")
                    ),
                    "from_session_db": True,  # маркер для диагностики
                }
                state.finished_at = session.get("summary_generated_at")
                logger.info(
                    f"Sprint 6: restored LLM summary for task={task.id} "
                    f"({len(session['summary_text'])} chars)"
                )
            except Exception as exc:
                logger.warning(
                    f"Sprint 6: restore summary for task={task.id} failed: {exc}"
                )

        # Восстанавливаем Q&A-историю, если она пустая
        if not has_qa and session.get("qa_history"):
            try:
                # Глубокая копия — чтобы избежать мутаций общих объектов
                task.llm_qa_history = list(session["qa_history"])
                logger.info(
                    f"Sprint 6: restored Q&A history for task={task.id} "
                    f"({len(task.llm_qa_history)} entries)"
                )
            except Exception as exc:
                logger.warning(
                    f"Sprint 6: restore qa_history for task={task.id} failed: {exc}"
                )
    except Exception as exc:
        logger.debug(f"Sprint 6: _try_restore_llm_session({task.id}) failed: {exc}")


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


def _touch_task_lru(task_id: str) -> None:
    """Помечает задачу как недавно использованную (LRU update).

    Используется внешними модулями (pipeline, analytics_ops) после
    обновления полей задачи — чтобы LRU не вытеснил активную задачу.
    """
    with _tasks_lock:
        if task_id in _tasks:
            _tasks.move_to_end(task_id)


def unregister_task(task_id: str, user_id: Optional[int] = None) -> bool:
    """Удаляет задачу из in-memory LRU-кэша _tasks.

    Используется repository.delete_task() — после удаления задачи из БД
    нужно также убрать её из _tasks, иначе list_user_tasks() добавит
    её обратно в список (т.к. она есть в памяти, но уже нет в БД —
    сработает логика "in-memory задача, которой нет в БД → свежая,
    добавить в начало списка").

    Args:
        task_id: id задачи для удаления из кэша.
        user_id: если передан — задача удаляется только если её user_id
            совпадает. Это защита от race condition: если задача уже
            вытеснена из кэша и на её место встала другая (с другим
            user_id), мы не должны удалить чужую задачу. Если None —
            удаляем без проверки (для cleanup-сценариев).

    Returns:
        True если задача была в кэше и удалена, False если её там не было
        или user_id не совпал.
    """
    with _tasks_lock:
        task = _tasks.get(task_id)
        if task is None:
            return False
        if user_id is not None and task.user_id != user_id:
            # Race condition: задача уже вытеснена/перезаписана — не трогаем.
            logger.warning(
                f"unregister_task({task_id}): user_id mismatch "
                f"(expected={user_id}, actual={task.user_id}) — пропускаем"
            )
            return False
        _tasks.pop(task_id, None)

    # Обновляем Prometheus gauge
    try:
        from ..middleware.metrics import update_tasks_in_memory
        update_tasks_in_memory(len(_tasks))
    except Exception:
        pass

    logger.info(f"unregister_task: task={task_id} удалена из _tasks")
    return True


def _now_utc() -> datetime:
    """Хелпер для общей временной метки (используется в нескольких модулях)."""
    return datetime.now(timezone.utc)


def _gen_task_id() -> str:
    """Генерирует короткий ID задачи (12 hex-символов)."""
    return uuid.uuid4().hex[:12]
