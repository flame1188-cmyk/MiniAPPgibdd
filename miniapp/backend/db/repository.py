"""
TaskRepository — CRUD задач выгрузки + аудит-лог обращений к ПДн.

Дизайн:
- Если PostgreSQL готов (is_db_ready() == True) — операции идут в БД,
  in-memory словарь _TASKS_HEAVY_STATE используется как кэш для тяжёлых полей
  (cards, raw_clusters и т.д.), которые не сериализуются в БД на Этапе 2.
- Task-объекты хранятся в task_registry._tasks (единственный in-memory кэш).
- Если PostgreSQL НЕ готов — задачи хранятся только в task_registry._tasks
  (теряются при рестарте, поведение идентично эпохе до подключения БД).

Это гарантирует, что:
1. При недоступности БД приложение не падает.
2. При рестарте с БД — задачи восстанавливаются (метаданные + files +
   analytics), но тяжёлые поля (cards, raw_clusters) нужно
   перезагрузить (через data_cache или повторный расчёт).
3. При множественных воркерах — метаданные консистентны (тяжёлые
   поля могут расходиться, но это решается на Этапе 3 кэшем карточек).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from psycopg import OperationalError
from psycopg.types.json import Json, Jsonb
from psycopg.rows import dict_row

from .connection import get_pool, is_db_ready

logger = logging.getLogger(__name__)

# ====================================================================
# In-memory кэш для тяжёлых полей Task
# ====================================================================
# Ключ: task_id, значение: dict с полями {cards, prev_cards, prev_label,
# prev_cards_loaded, comparison, clusters_state, llm_summary_state,
# llm_qa_history, last_point_stats, raw_clusters, raw_preclusters,
# last_point_cards_current, last_point_cards_prev, last_point_params}
#
# Зачем: эти поля не персистятся в БД на Этапе 2 (слишком большие),
# но нужны для работы analytics/clusters/point_stats/LLM. При рестарте
# они теряются — пользователь может либо пере-открыть вкладку (тогда
# данные перезагружаются лениво через ensure_prev_cards и т.д.),
# либо пересоздать задачу.
_TASKS_HEAVY_STATE: Dict[str, Dict[str, Any]] = {}

# NOTE: _TASKS_MEMORY удалён (P1 cache consolidation).
# Все in-memory операции с задачами теперь идут через task_registry._tasks
# (единственный источник правды для in-memory кэша задач).


# ====================================================================
# Retry-обёртка для операций с пулом PostgreSQL.
# ====================================================================
# Проблема: даже с check= и check_interval= в пуле иногда остаются
# BAD-соединения (гонка: соединение умерло после последнего
# check_connection, но до выдачи из getconn()). В этом случае первый
# запрос падает с OperationalError / "SSL error: unexpected eof".
#
# Решение: одна попытка retry. При возврате соединения через __exit__
# пул сам его выбросит (BAD), поэтому повторный вызов получит свежее.
#
# Обёртка применяется только к read-only функциям, которые фигурируют
# в логах WARNING (list_user_tasks_from_db, recover_stale_pending).
# Write-функции (save_task) ретраятся на уровне router'а.
async def _with_pool_retry(coro_fn, *args, **kwargs):
    """
    Вызывает async-функцию coro_fn(*args, **kwargs) с одним retry
    при OperationalError. Используется только для read-only операций
    (для write нужна идемпотентность — см. routers/dtp.py).

    Возвращает (success: bool, result_or_exception).
    """
    try:
        return True, await coro_fn(*args, **kwargs)
    except OperationalError as exc:
        # Пул выбросит BAD-соединение при возврате через __exit__,
        # поэтому повторный вызов получит свежее.
        logger.warning(
            f"_with_pool_retry: DB op failed with OperationalError, "
            f"retrying once: {exc}"
        )
    try:
        return True, await coro_fn(*args, **kwargs)
    except OperationalError as exc:
        logger.warning(f"_with_pool_retry: retry also failed: {exc}")
        return False, exc


def set_heavy_state(task_id: str, key: str, value: Any) -> None:
    """Сохраняет тяжёлое поле Task в in-memory кэше."""
    if task_id not in _TASKS_HEAVY_STATE:
        _TASKS_HEAVY_STATE[task_id] = {}
    _TASKS_HEAVY_STATE[task_id][key] = value


def get_heavy_state(task_id: str, key: str, default: Any = None) -> Any:
    """Достаёт тяжёлое поле Task из in-memory кэша."""
    return _TASKS_HEAVY_STATE.get(task_id, {}).get(key, default)


def drop_heavy_state(task_id: str) -> None:
    """Удаляет весь тяжёлый state задачи (при cleanup)."""
    _TASKS_HEAVY_STATE.pop(task_id, None)


# ====================================================================
# Сохранение задачи в БД
# ====================================================================
async def save_task(task: Any) -> None:
    """
    Сохраняет метаданные задачи в БД (INSERT или UPDATE по id).

    Тяжёлые поля (cards, raw_clusters и т.д.) НЕ сохраняются —
    они остаются in-memory через set_heavy_state().
    """
    # Сохраняем тяжёлые поля в memory-кэш (всегда, даже если БД есть)
    _cache_heavy_fields(task)

    if not is_db_ready():
        # БД нет — fallback: только кэшируем тяжёлые поля.
        # Task-объект уже в task_registry._tasks через _register_task().
        return

    pool = get_pool()
    if pool is None:
        return

    try:
        async with pool.connection() as conn:
            # upsert: INSERT ... ON CONFLICT (id) DO UPDATE
            await conn.execute(
                """
                INSERT INTO tasks (
                    id, user_id, region_code, region_name, period_label,
                    dat_list, raw_query, status, progress, error,
                    total_dtp, total_dead, total_injured, files,
                    analytics, clusters_result, created_at, updated_at
                ) VALUES (
                    %(id)s, %(user_id)s, %(region_code)s, %(region_name)s,
                    %(period_label)s, %(dat_list)s, %(raw_query)s,
                    %(status)s, %(progress)s, %(error)s,
                    %(total_dtp)s, %(total_dead)s, %(total_injured)s,
                    %(files)s, %(analytics)s, %(clusters_result)s,
                    %(created_at)s, %(updated_at)s
                )
                ON CONFLICT (id) DO UPDATE SET
                    status = EXCLUDED.status,
                    progress = EXCLUDED.progress,
                    error = EXCLUDED.error,
                    total_dtp = EXCLUDED.total_dtp,
                    total_dead = EXCLUDED.total_dead,
                    total_injured = EXCLUDED.total_injured,
                    files = EXCLUDED.files,
                    analytics = COALESCE(EXCLUDED.analytics, tasks.analytics),
                    clusters_result = COALESCE(
                        EXCLUDED.clusters_result, tasks.clusters_result
                    ),
                    updated_at = NOW()
                """,
                params={
                    "id": task.id,
                    "user_id": task.user_id,
                    "region_code": task.region_code,
                    "region_name": task.region_name,
                    "period_label": task.period_label,
                    "dat_list": Json(task.dat_list),
                    "raw_query": task.raw_query,
                    "status": task.status.value
                    if hasattr(task.status, "value")
                    else str(task.status),
                    "progress": task.progress,
                    "error": task.error,
                    "total_dtp": task.total_dtp,
                    "total_dead": task.total_dead,
                    "total_injured": task.total_injured,
                    "files": Json(task.files),
                    "analytics": Json(task.analytics)
                    if task.analytics is not None
                    else None,
                    "clusters_result": Json(task.clusters_state.result)
                    if task.clusters_state
                    and task.clusters_state.result is not None
                    else None,
                    "created_at": task.created_at,
                    "updated_at": task.updated_at,
                },
            )
            await conn.commit()

        # Task-объект уже в task_registry._tasks — мутации видны через
        # ту же ссылку. Дублирующая запись не нужна.

    except Exception as exc:
        logger.warning(
            f"save_task({task.id}) failed: {exc}"
        )


# ====================================================================
# Загрузка задачи из БД
# ====================================================================
async def load_task(task_id: str, task_factory: Any) -> Optional[Any]:
    """
    Загружает задачу по id.

    Сначала проверяет in-memory кэш (быстро + содержит тяжёлые поля).
    Если нет — идёт в БД (если готова) и конструирует Task из строки.
    Если нет нигде — None.

    task_factory: callable(id, user_id, region_code, region_name,
                           period_label, dat_list, raw_query) -> Task
    Используется для создания объекта Task без циклического импорта.
    """
    # In-memory check делегирован в вызывающий get_task_async()

    if not is_db_ready():
        return None

    pool = get_pool()
    if pool is None:
        return None

    try:
        async with pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT id, user_id, region_code, region_name, period_label,
                       dat_list, raw_query, status, progress, error,
                       total_dtp, total_dead, total_injured, files,
                       analytics, clusters_result,
                       created_at, updated_at
                FROM tasks WHERE id = %(id)s
                """,
                params={"id": task_id},
                prepare=False,
            )
            row = await cur.fetchone()

        if row is None:
            return None

        # Создаём Task через factory (избегаем циклического импорта)
        task = task_factory(
            id=row["id"],
            user_id=row["user_id"],
            region_code=row["region_code"],
            region_name=row["region_name"],
            period_label=row["period_label"],
            dat_list=list(row["dat_list"]) if row["dat_list"] else [],
            raw_query=row["raw_query"] or "",
        )

        # Восстанавливаем сохранённые поля
        _restore_status(task, row["status"], row["progress"], row["error"])
        task.total_dtp = row["total_dtp"] or 0
        task.total_dead = row["total_dead"] or 0
        task.total_injured = row["total_injured"] or 0
        task.files = list(row["files"]) if row["files"] else []
        task.analytics = row["analytics"]
        if (
            row["clusters_result"]
            and task.clusters_state
        ):
            task.clusters_state.result = row["clusters_result"]
            task.clusters_state.status = _make_analysis_status("done")
            task.clusters_state.progress = 100
            task.clusters_state.stage = "Готово (восстановлено из БД)"

        # created_at/updated_at из БД
        if row["created_at"]:
            task.created_at = row["created_at"]
        if row["updated_at"]:
            task.updated_at = row["updated_at"]

        # Caller (get_task_async) зарегистрирует в task_registry._tasks
        # через _register_task() — кэширование здесь не нужно.
        return task

    except Exception as exc:
        logger.warning(f"load_task({task_id}) failed: {exc}")
        return None


def _restore_status(task: Any, status: str, progress: int, error: Optional[str]) -> None:
    """Восстанавливает статус задачи из строкового представления."""
    # TaskStatus — Enum, ищем по value
    try:
        from ..services.gibdd_service import TaskStatus

        for s in TaskStatus:
            if s.value == status:
                task.status = s
                break
    except Exception:
        pass
    task.progress = progress or 0
    task.error = error


def _make_analysis_status(value: str):
    """Создаёт AnalysisStatus из строкового значения."""
    try:
        from ..services.gibdd_service import AnalysisStatus

        for s in AnalysisStatus:
            if s.value == value:
                return s
    except Exception:
        pass
    return None


# ====================================================================
# Список задач пользователя
# ====================================================================
async def list_user_tasks_from_db(
    user_id: int, limit: int, task_factory: Any
) -> List[Any]:
    """
    Возвращает последние N задач пользователя (из БД).
    Если БД недоступна — fallback на in-memory.

    Phase C.3 hotfix: перед SELECT лениво вызывает
    _maybe_recover_stale_pending_tasks() (TTL 60 сек) — это помечает
    "ghost"-задачи (stale pending) как failed/done, чтобы пользователь
    увидел cleanup в списке без ожидания рестарта сервера.
    """
    # Lazy-import task_registry._tasks для замены _TASKS_MEMORY
    # (единственный in-memory кэш задач).
    try:
        from ..services.task_registry import _tasks as _reg_tasks
        from ..services.task_registry import _register_task as _reg_register
    except Exception:
        _reg_tasks = {}  # type: ignore[assignment]
        async def _noop(t): pass  # type: ignore[assignment]
        _reg_register = _noop

    # Phase C.3 hotfix: lazy cleanup ghost-задач (TTL-protected)
    try:
        await _maybe_recover_stale_pending_tasks()
    except Exception as exc:
        logger.debug(f"list_user_tasks_from_db: stale recovery skipped: {exc}")

    if not is_db_ready():
        # In-memory fallback
        user_tasks = [
            t for t in _reg_tasks.values() if t.user_id == user_id
        ]
        user_tasks.sort(key=lambda t: t.created_at, reverse=True)
        return user_tasks[:limit]

    pool = get_pool()
    if pool is None:
        return []

    # Inline retry по OperationalError. Пул может выдать BAD-соединение
    # (гонка между check_connection и getconn), в этом случае первый
    # вызов падает с OperationalError/"SSL eof". При возврате через
    # __exit__ пул выбросит BAD, и повторная попытка получит свежее.
    last_exc: Optional[Exception] = None
    for attempt in (1, 2):
        try:
            async with pool.connection() as conn:
                cur = await conn.execute(
                    """
                    SELECT id, user_id, region_code, region_name, period_label,
                           dat_list, raw_query, status, progress, error,
                           total_dtp, total_dead, total_injured, files,
                           analytics, clusters_result,
                           created_at, updated_at
                    FROM tasks
                    WHERE user_id = %(uid)s
                    ORDER BY created_at DESC
                    LIMIT %(limit)s
                    """,
                    params={"uid": user_id, "limit": limit},
                    prepare=False,
                )
                rows = await cur.fetchall()

            tasks: List[Any] = []
            for row in rows:
                # Проверяем in-memory кэш (чтобы вернуть тяжёлые поля, если они есть)
                if row["id"] in _reg_tasks:
                    tasks.append(_reg_tasks[row["id"]])
                    continue

                task = task_factory(
                    id=row["id"],
                    user_id=row["user_id"],
                    region_code=row["region_code"],
                    region_name=row["region_name"],
                    period_label=row["period_label"],
                    dat_list=list(row["dat_list"]) if row["dat_list"] else [],
                    raw_query=row["raw_query"] or "",
                )
                _restore_status(task, row["status"], row["progress"], row["error"])
                task.total_dtp = row["total_dtp"] or 0
                task.total_dead = row["total_dead"] or 0
                task.total_injured = row["total_injured"] or 0
                task.files = list(row["files"]) if row["files"] else []
                task.analytics = row["analytics"]
                if row["clusters_result"] and task.clusters_state:
                    task.clusters_state.result = row["clusters_result"]
                    task.clusters_state.status = _make_analysis_status("done")
                    task.clusters_state.progress = 100
                    task.clusters_state.stage = "Готово (восстановлено из БД)"

                if row["created_at"]:
                    task.created_at = row["created_at"]
                if row["updated_at"]:
                    task.updated_at = row["updated_at"]

                await _reg_register(task)
                tasks.append(task)

            return tasks

        except OperationalError as exc:
            last_exc = exc
            if attempt == 1:
                logger.warning(
                    f"list_user_tasks_from_db: OperationalError on attempt 1, "
                    f"retrying: {exc}"
                )
                continue
            logger.warning(f"list_user_tasks_from_db: retry also failed: {exc}")
            break
        except Exception as exc:
            logger.warning(f"list_user_tasks_from_db failed: {exc}")
            break

    # In-memory fallback (после retry-exhaustion или не-DB ошибки)
    user_tasks = [t for t in _reg_tasks.values() if t.user_id == user_id]
    user_tasks.sort(key=lambda t: t.created_at, reverse=True)
    return user_tasks[:limit]


# ====================================================================
# Удаление старых задач
# ====================================================================
async def delete_old_tasks(
    max_age_hours: int, project_root: Path
) -> int:
    """
    Удаляет задачи старше max_age_hours.

    Удаляет из:
    - in-memory кэша (task_registry._tasks и _TASKS_HEAVY_STATE)
    - БД (если доступна)
    - диска (data/tasks/{task_id}/)

    Возвращает количество удалённых задач.
    """
    now = datetime.now(timezone.utc)
    cutoff_ts = now.timestamp() - max_age_hours * 3600

    # 1. Собираем кандидатов на удаление из in-memory
    try:
        from ..services.task_registry import _tasks as _del_tasks
    except Exception:
        _del_tasks = {}  # type: ignore[assignment]
    to_delete_memory = [
        tid
        for tid, task in _del_tasks.items()
        if task.created_at.timestamp() < cutoff_ts
    ]

    # 2. Если БД есть — собираем кандидатов и оттуда
    db_deleted = 0
    if is_db_ready():
        pool = get_pool()
        if pool is not None:
            try:
                async with pool.connection() as conn:
                    # Сначала выбираем id задач для удаления файлов
                    cur = await conn.execute(
                        """
                        SELECT id, files FROM tasks
                        WHERE created_at < NOW() - (%(hours)s || ' hours')::INTERVAL
                        """,
                        params={"hours": str(max_age_hours)},
                        prepare=False,
                    )
                    rows = await cur.fetchall()

                    # Удаляем файлы с диска для найденных задач
                    for row in rows:
                        tid = row["id"]
                        files = row["files"] or []
                        for f in files:
                            try:
                                Path(f.get("path", "")).unlink(missing_ok=True)
                            except Exception:
                                pass
                        # Удаляем директорию задачи
                        try:
                            task_dir = project_root / "data" / "tasks" / tid
                            if task_dir.exists():
                                task_dir.rmdir()
                        except Exception:
                            pass

                    # Удаляем сами строки из БД
                    if rows:
                        ids_to_delete = [r["id"] for r in rows]
                        await conn.execute(
                            "DELETE FROM tasks WHERE id = ANY(%s)",
                            params=(ids_to_delete,),
                        )
                        await conn.commit()
                        db_deleted = len(ids_to_delete)

            except Exception as exc:
                logger.warning(f"delete_old_tasks (DB) failed: {exc}")

    # 3. In-memory cleanup
    memory_deleted = 0
    for tid in to_delete_memory:
        # Получаем task ДО удаления из кэша (исправление бага:
        # раньше было .pop() затем .get() → всегда None).
        task = _del_tasks.get(tid)
        _del_tasks.pop(tid, None)
        drop_heavy_state(tid)
        if task:
            for f in task.files:
                try:
                    Path(f.get("path", "")).unlink(missing_ok=True)
                except Exception:
                    pass
            try:
                task_dir = project_root / "data" / "tasks" / tid
                if task_dir.exists():
                    task_dir.rmdir()
            except Exception:
                pass
        memory_deleted += 1

    total = max(db_deleted, memory_deleted)
    if total > 0:
        logger.info(
            f"Cleanup: удалено {total} старых задач "
            f"(db={db_deleted}, memory={memory_deleted})"
        )
    return total


async def delete_task(
    task_id: str,
    user_id: int,
    project_root: Optional[Path] = None,
) -> bool:
    """Удаляет одну задачу пользователя (по требованию пользователя).

    Удаляет из:
    - БД (только если task.user_id == user_id — защита от удаления чужих задач)
    - in-memory кэша (task_registry._tasks и _TASKS_HEAVY_STATE)
    - диска (data/tasks/{task_id}/ и все файлы задачи из task.files[].path)

    Args:
        task_id: id задачи для удаления.
        user_id: id пользователя, инициатора удаления. Если не совпадает
            с task.user_id в БД — удаление отменяется, возвращается False.
        project_root: корень проекта (для поиска data/tasks/). Если None —
            ищётся относительно текущего файла (../../..).

    Returns:
        True если задача была найдена и удалена,
        False если не найдена или user_id не совпал.
    """
    if project_root is None:
        # repository.py → miniapp/backend/db/ → miniapp/backend/ → miniapp/ → project_root
        project_root = Path(__file__).resolve().parents[3]

    deleted = False

    # 1. БД: SELECT for ownership check + получаем files для удаления с диска
    if is_db_ready():
        pool = get_pool()
        if pool is not None:
            try:
                async with pool.connection() as conn:
                    # Сначала проверяем ownership и собираем files
                    cur = await conn.execute(
                        """
                        SELECT id, files FROM tasks
                        WHERE id = %s AND user_id = %s
                        """,
                        params=(task_id, user_id),
                    )
                    row = await cur.fetchone()

                    if row is None:
                        # Либо задачи нет, либо чужая — возвращаем False
                        # без раскрытия, какая именно (безопасность).
                        return False

                    files = row["files"] or []

                    # Удаляем файлы с диска для найденной задачи
                    for f in files:
                        try:
                            Path(f.get("path", "")).unlink(missing_ok=True)
                        except Exception:
                            pass
                    # Удаляем директорию задачи
                    try:
                        task_dir = project_root / "data" / "tasks" / task_id
                        if task_dir.exists():
                            task_dir.rmdir()
                    except Exception:
                        pass

                    # Удаляем строку из БД
                    await conn.execute(
                        "DELETE FROM tasks WHERE id = %s AND user_id = %s",
                        params=(task_id, user_id),
                    )
                    await conn.commit()
                    deleted = True
            except Exception as exc:
                logger.warning(f"delete_task({task_id}) DB failed: {exc}")
                # Не возвращаемся — пробуем хотя бы почистить in-memory

    # 2. In-memory cleanup + heavy state.
    # unregister_task() (блок 3 ниже) удалит из task_registry._tasks.
    # Здесь чистим _TASKS_HEAVY_STATE и файлы на диске.
    try:
        from ..services.task_registry import _tasks as _dt_tasks
    except Exception:
        _dt_tasks = {}  # type: ignore[assignment]
    task = _dt_tasks.get(task_id)
    if task is not None and task.user_id == user_id:
        for f in task.files:
            try:
                Path(f.get("path", "")).unlink(missing_ok=True)
            except Exception:
                pass
        try:
            task_dir = project_root / "data" / "tasks" / task_id
            if task_dir.exists():
                task_dir.rmdir()
        except Exception:
            pass
        drop_heavy_state(task_id)
        deleted = True

    # 3. Чистим task_registry._tasks (LRU-кэш — единый in-memory источник).
    # Это критично: pre-check в роутере вызывает get_task_async(), который
    # через _register_task() кладёт задачу в _tasks. Если её не убрать —
    # list_user_tasks() добавит её обратно в список ("в памяти, но не в БД
    # → свежая → вставить в начало").
    try:
        from ..services.task_registry import unregister_task
        if await unregister_task(task_id, user_id=user_id):
            deleted = True
    except Exception as exc:
        logger.warning(f"delete_task({task_id}) unregister_task failed: {exc}")

    if deleted:
        logger.info(
            f"delete_task: task={task_id} user={user_id} — удалена "
            f"(db={'да' if is_db_ready() else 'нет'})"
        )
    return deleted


# ====================================================================
# Аудит-лог обращений к ПДн (152-ФЗ)
# ====================================================================
async def log_access(
    user_id: int,
    action: str,
    region_code: Optional[str] = None,
    period_label: Optional[str] = None,
    task_id: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Логирует обращение пользователя к данным ДТП.

    См. миниapp/README.md → «Требования 152-ФЗ»:
    «Журнал аудита доступа к ПДн (логировать все запросы
    user_id → region_code, period)».

    Если БД недоступна — запись логируется только в обычный логгер
    (теряется при рестарте, но не роняет приложение).
    """
    if not is_db_ready():
        logger.info(
            f"ACCESS_LOG (in-memory): user={user_id} action={action} "
            f"region={region_code} period={period_label} task={task_id}"
        )
        return

    pool = get_pool()
    if pool is None:
        return

    try:
        async with pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO access_log (
                    user_id, region_code, period_label, action, task_id, details
                ) VALUES (
                    %(uid)s, %(reg)s, %(period)s, %(action)s, %(tid)s, %(details)s
                )
                """,
                params={
                    "uid": user_id,
                    "reg": region_code,
                    "period": period_label,
                    "action": action,
                    "tid": task_id,
                    "details": Json(details) if details else None,
                },
            )
            await conn.commit()
    except Exception as exc:
        logger.warning(f"log_access failed: {exc}")


# ====================================================================
# Вспомогательные: сохранение/восстановление тяжёлых полей
# ====================================================================
# Список полей Task, которые НЕ персистятся в БД на Этапе 2.
# Они остаются in-memory, чтобы не раздувать JSONB-колонки.
# Этап 3 (cards cache) и Этап 4 (clusters history) закроют их отдельно.
_HEAVY_FIELDS = (
    "cards",
    "prev_cards",
    "prev_label",
    "prev_cards_loaded",
    "comparison",
    "llm_summary_state",
    "llm_qa_history",
    "last_point_stats",
    "raw_clusters",
    "raw_preclusters",
    "last_point_cards_current",
    "last_point_cards_prev",
    "last_point_params",
)


def _cache_heavy_fields(task: Any) -> None:
    """Копирует тяжёлые поля Task в in-memory кэш."""
    cache = _TASKS_HEAVY_STATE.setdefault(task.id, {})
    for field_name in _HEAVY_FIELDS:
        if hasattr(task, field_name):
            cache[field_name] = getattr(task, field_name)


def attach_heavy_state(task: Any) -> None:
    """
    Присоединяет к Task тяжёлые поля из кэша (если они есть).
    Вызывается после load_task, чтобы восстановить состояние.
    """
    cache = _TASKS_HEAVY_STATE.get(task.id)
    if not cache:
        return
    for field_name in _HEAVY_FIELDS:
        if field_name in cache and hasattr(task, field_name):
            # Не затираем поле, если оно уже заполнено
            # (например, после ensure_prev_cards)
            current = getattr(task, field_name)
            if not current and cache[field_name]:
                setattr(task, field_name, cache[field_name])


# ====================================================================
# Sprint 5: Task recovery на startup
# ====================================================================
# При рестарте сервера in-flight задачи (status='fetching'/'analytics'
# 'running') остаются в этом статусе вечно — рабочий процесс,
# который их обрабатывал, умер вместе с сервером.
# Эта функция находит такие задачи в БД и помечает их как failed с понятным
# сообщением, чтобы пользователь увидел ошибку и мог пересоздать задачу
# вместо бесконечного ожидания.
_INCOMPLETE_STATUSES = ("fetching", "analytics", "running")


async def recover_incomplete_tasks() -> int:
    """
    Sprint 5: помечает незавершённые задачи как failed.

    Вызывается один раз при старте сервера (после init_pool).
    Возвращает количество восстановленных задач.

    Логика:
      - status IN (fetching, analytics, running) → failed
      - error = 'Прервано рестартом сервера (Sprint 5 recovery)'
      - progress не трогаем (полезно для отладки — видно, где оборвалось)
      - clusters_state.status='running' / llm_summary_state.status='running'
        тоже помечаем как failed (тяжёлые state-объекты лежат в БД только
        частично — JSONB-колонки clusters_result и т.д., но status-строка
        в самих колонках не персистится; здесь работает только на in-memory).
    """
    if not is_db_ready():
        # Без БД — in-memory задачи и так пусты после рестарта.
        return 0

    pool = get_pool()
    if pool is None:
        return 0

    recovered_count = 0
    try:
        async with pool.connection() as conn:
            # Сначала собираем ID задач для логирования
            cur = await conn.execute(
                """
                SELECT id, status, progress FROM tasks
                WHERE status = ANY(%(statuses)s)
                """,
                params={"statuses": list(_INCOMPLETE_STATUSES)},
                prepare=False,
            )
            rows = await cur.fetchall()

            if not rows:
                return 0

            # UPDATE одним запросом — помечаем все как failed
            await conn.execute(
                """
                UPDATE tasks
                SET status = 'failed',
                    error = %(error_msg)s,
                    updated_at = NOW()
                WHERE status = ANY(%(statuses)s)
                """,
                params={
                    "error_msg": "Прервано рестартом сервера (Sprint 5 recovery)",
                    "statuses": list(_INCOMPLETE_STATUSES),
                },
            )
            await conn.commit()
            recovered_count = len(rows)

        # Логируем каждую восстановленную задачу
        for row in rows:
            logger.warning(
                f"Sprint 5 recovery: task {row['id']} "
                f"was status='{row['status']}' progress={row['progress']} "
                f"→ marked as failed (server restart)"
            )

        # Также чистим in-memory кэш от мёртвых задач
        try:
            from ..services.task_registry import _tasks as _ri_tasks
        except Exception:
            _ri_tasks = {}  # type: ignore[assignment]
        for tid in list(_ri_tasks.keys()):
            task = _ri_tasks[tid]
            try:
                if hasattr(task, "status") and hasattr(task.status, "value"):
                    if task.status.value in _INCOMPLETE_STATUSES:
                        # Не удаляем из памяти — оставляем с пометкой failed,
                        # чтобы пользователь увидел ошибку в UI.
                        from ..services.gibdd_service import TaskStatus
                        task.status = TaskStatus.FAILED
                        task.error = (
                            "Прервано рестартом сервера (Sprint 5 recovery)"
                        )
            except Exception:
                pass

        if recovered_count:
            logger.info(
                f"Sprint 5 recovery: {recovered_count} incomplete tasks "
                f"marked as failed"
            )

    except Exception as exc:
        logger.warning(f"Sprint 5 recovery failed: {exc}")

    return recovered_count


# ====================================================================
# Phase C.3 hotfix: восстановление "ghost"-задач (stale pending)
# ====================================================================
# После фикса _TaskStub AttributeError (предыдущая итерация Phase C.3)
# старые pre-fix задачи оказались в подвешенном состоянии:
#   - DB: status='pending', progress=0, files=[]
#   - Redis: snapshot отсутствует (воркер не смог сохранить из-за _TaskStub)
#   - in-memory _tasks: пусто (сброшено при redeploy)
#
# Существующая recover_incomplete_tasks() НЕ ловит их — она ищет только
# fetching/analytics/running, а pending считается
# легитимным начальным состоянием (задача только что создана, ещё в очереди).
#
# Эта функция определяет "stale pending" как:
#   - status='pending' AND progress=0
#   - created_at < NOW() - INTERVAL 'N minutes'  (N = GIBDD_STALE_PENDING_MINUTES, по умолч. 15)
#
# Для каждой такой задачи пытается восстановить реальное состояние:
#   1. Проверяем Redis snapshot — если есть с status='done', переносим
#      финальные поля (status, progress, files, total_*, analytics) в БД.
#      Это случай, когда воркер УСПЕШНО выполнил pipeline, но БД не была
#      обновлена (Celery-воркер не пишет в БД напрямую — только в Redis).
#   2. Если Redis snapshot отсутствует или не 'done' — проверяем диск
#      на наличие файлов (data/tasks/{task_id}/dtp_cards_*.xlsx).
#      Если файлы есть — pipeline фактически завершился, восстанавливаем
#      как done с метаданными файлов, найденными на диске.
#   3. Если ни Redis, ни диск не дают данных — помечаем как failed с
#      понятным сообщением "Задача прервана (перезапуск сервера во время
#      выполнения)".
#
# Вызывается:
#   - При старте сервера (main.py, после recover_incomplete_tasks)
#   - Lazily в list_user_tasks_from_db (через TTL-кэш 60 сек) — чтобы
#     пользователь увидел cleanup без ожидания рестарта
# ====================================================================

# Порог staleness в минутах (env-configurable для тонкой настройки).
import os as _os
try:
    _STALE_PENDING_MINUTES = int(
        _os.environ.get("GIBDD_STALE_PENDING_MINUTES", "15")
    )
    if _STALE_PENDING_MINUTES < 1:
        _STALE_PENDING_MINUTES = 15
except Exception:
    _STALE_PENDING_MINUTES = 15

# TTL для lazy-вызова в list_user_tasks_from_db — чтобы не дёргать
# recover_stale_pending_tasks() на каждый GET /tasks.
_STALE_RECOVERY_TTL_SECONDS = 60
_last_stale_recovery_at: Optional[datetime] = None


def _find_task_files_on_disk(task_id: str) -> List[Dict[str, Any]]:
    """Сканирует data/tasks/{task_id}/ на наличие сгенерированных файлов.

    Возвращает список метаданных файлов в формате, совместимом с
    task.files (для записи в БД). Если директория не существует или
    пуста — возвращает пустой список.

    Файлы ищутся по glob-шаблонам:
      - dtp_cards_*.xlsx  → type=dtp_cards
      - dtp_uch_*.xlsx    → type=dtp_participants
      - dtp_map_*.html    → type=map_html
    """
    try:
        from ..services._imports import _PROJECT_ROOT
    except Exception:
        return []

    task_dir = _PROJECT_ROOT / "data" / "tasks" / task_id
    if not task_dir.is_dir():
        return []

    files_meta: List[Dict[str, Any]] = []
    mime_map = {
        "dtp_cards": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "dtp_participants": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "map_html": "text/html",
    }

    patterns = [
        ("dtp_cards", "dtp_cards_*.xlsx"),
        ("dtp_participants", "dtp_uch_*.xlsx"),
        ("map_html", "dtp_map_*.html"),
    ]

    for ftype, pattern in patterns:
        matches = sorted(task_dir.glob(pattern))
        if not matches:
            continue
        # Берём первый (на случай дубликатов — маловероятно, но безопасно)
        path = matches[0]
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size == 0:
            continue  # пустой файл — игнорируем
        files_meta.append({
            "type": ftype,
            "filename": path.name,
            "path": str(path),
            "size_bytes": size,
            "mime": mime_map.get(ftype, "application/octet-stream"),
        })

    return files_meta


async def recover_stale_pending_tasks() -> int:
    """Помечает "stale pending" ghost-задачи как failed или done.

    См. подробное описание выше (_STALE_PENDING_MINUTES, логика
    Redis-snapshot → disk-files → failed fallback).

    Returns:
        Количество задач, для которых было изменено состояние в БД.
    """
    global _last_stale_recovery_at
    _last_stale_recovery_at = datetime.now(timezone.utc)

    if not is_db_ready():
        return 0
    pool = get_pool()
    if pool is None:
        return 0

    # Retry по OperationalError через _with_pool_retry. Пул может
    # выдать BAD-соединение (гонка между check_connection и getconn),
    # тогда первый вызов падает с "SSL eof". При возврате через __exit__
    # пул выбросит BAD, и повторная попытка получит свежее. Функция
    # идемпотентна: повторный SELECT найдёт уже помеченные задачи
    # и пропустит их (UPDATE WHERE status='pending' не заденет failed/done).
    success, result = await _with_pool_retry(
        _recover_stale_pending_tasks_impl, pool
    )
    if success:
        return result  # int (recovered_count)
    return 0  # fallback после retry-exhaustion


async def _recover_stale_pending_tasks_impl(pool) -> int:
    """Реализация recover_stale_pending_tasks с одним соединением.

    Вызывается через _with_pool_retry для retry по OperationalError.
    Идемпотентна: повторный запуск найдёт уже помеченные задачи
    и пропустит их (UPDATE WHERE status='pending' не заденет failed/done).
    """
    recovered_count = 0
    try:
        async with pool.connection() as conn:
            # Находим все stale pending задачи
            cur = await conn.execute(
                """
                SELECT id, status, progress, created_at
                FROM tasks
                WHERE status = 'pending'
                  AND progress = 0
                  AND created_at < NOW() - (%s || ' minutes')::interval
                """,
                params=(_STALE_PENDING_MINUTES,),
                prepare=False,
            )
            rows = await cur.fetchall()

            if not rows:
                return 0

            # Логируем обнаруженные ghost-задачи
            logger.info(
                f"recover_stale_pending: обнаружено {len(rows)} stale pending "
                f"задач (старше {_STALE_PENDING_MINUTES} мин) — восстановление"
            )

            # Lazy import worker.task_state (может быть недоступен в тестах)
            load_task_state_fn = None
            try:
                from worker.task_state import load_task_state
                load_task_state_fn = load_task_state
            except Exception:
                pass

            tasks_to_fail: List[str] = []
            tasks_to_complete_from_snapshot: List[tuple] = []  # (id, snapshot)
            tasks_to_complete_from_disk: List[tuple] = []  # (id, files_meta)

            for row in rows:
                tid = row["id"]

                # 1. Проверяем Redis snapshot
                snapshot = None
                if load_task_state_fn is not None:
                    try:
                        snapshot = load_task_state_fn(tid)
                    except Exception as exc:
                        logger.debug(
                            f"recover_stale_pending({tid}): "
                            f"load_task_state failed: {exc}"
                        )

                if snapshot and isinstance(snapshot, dict) and \
                        snapshot.get("status") == "done":
                    # Задача фактически завершилась — переносим финал в БД
                    tasks_to_complete_from_snapshot.append((tid, snapshot))
                    logger.info(
                        f"recover_stale_pending({tid}): Redis snapshot имеет "
                        f"status=done — восстановление как done из snapshot"
                    )
                    continue

                # 2. Проверяем файлы на диске
                disk_files = _find_task_files_on_disk(tid)
                if disk_files:
                    # Файлы есть → pipeline завершился, но snapshot потерян
                    tasks_to_complete_from_disk.append((tid, disk_files))
                    logger.info(
                        f"recover_stale_pending({tid}): найдено "
                        f"{len(disk_files)} файлов на диске — восстановление "
                        f"как done"
                    )
                    continue

                # 3. Нет ни snapshot, ни файлов → помечаем как failed
                tasks_to_fail.append(tid)
                logger.warning(
                    f"recover_stale_pending({tid}): ни Redis snapshot, ни "
                    f"файлов на диске — помечаем как failed "
                    f"(прервано рестартом сервера)"
                )

            # Применяем обновления к БД
            if tasks_to_fail:
                await conn.execute(
                    """
                    UPDATE tasks
                    SET status = 'failed',
                        error = %s,
                        updated_at = NOW()
                    WHERE id = ANY(%s)
                    """,
                    params=[
                        "Задача прервана (перезапуск сервера во время выполнения)",
                        tasks_to_fail,
                    ],
                )
                recovered_count += len(tasks_to_fail)

            for tid, snap in tasks_to_complete_from_snapshot:
                files_json = snap.get("files") or []
                analytics_val = snap.get("analytics")
                await conn.execute(
                    """
                    UPDATE tasks
                    SET status = 'done',
                        progress = 100,
                        total_dtp = %s,
                        total_dead = %s,
                        total_injured = %s,
                        files = %s,
                        analytics = COALESCE(%s, analytics),
                        error = NULL,
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    params=[
                        int(snap.get("total_dtp") or 0),
                        int(snap.get("total_dead") or 0),
                        int(snap.get("total_injured") or 0),
                        Jsonb(files_json),
                        Jsonb(analytics_val) if analytics_val else None,
                        tid,
                    ],
                )
                recovered_count += 1

            for tid, files_meta in tasks_to_complete_from_disk:
                await conn.execute(
                    """
                    UPDATE tasks
                    SET status = 'done',
                        progress = 100,
                        files = %s,
                        error = NULL,
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    params=[Jsonb(files_meta), tid],
                )
                recovered_count += 1

            await conn.commit()

            # Логируем итог
            if recovered_count > 0:
                logger.info(
                    f"recover_stale_pending: восстановлено {recovered_count} "
                    f"задач "
                    f"(failed={len(tasks_to_fail)}, "
                    f"done_from_snapshot={len(tasks_to_complete_from_snapshot)}, "
                    f"done_from_disk={len(tasks_to_complete_from_disk)})"
                )

            # Также чистим in-memory кэш от ghost-задач
            try:
                from ..services.task_registry import _tasks as _rs_tasks
            except Exception:
                _rs_tasks = {}  # type: ignore[assignment]
            for tid in list(_rs_tasks.keys()):
                task = _rs_tasks[tid]
                try:
                    if hasattr(task, "status") and hasattr(task.status, "value"):
                        if task.status.value == "pending" and task.progress == 0:
                            # Проверяем created_at — задача должна быть stale
                            if task.created_at:
                                age = (
                                    datetime.now(timezone.utc) - task.created_at
                                ).total_seconds() / 60
                                if age > _STALE_PENDING_MINUTES:
                                    # Помечаем в in-memory тоже
                                    from ..services.gibdd_service import TaskStatus
                                    task.status = TaskStatus.FAILED
                                    task.error = (
                                        "Задача прервана (перезапуск сервера "
                                        "во время выполнения)"
                                    )
                except Exception:
                    pass

    except OperationalError:
        # Re-raise для _with_pool_retry — чтобы retry отработал
        raise
    except Exception as exc:
        logger.warning(f"_recover_stale_pending_tasks_impl failed: {exc}")

    return recovered_count


async def _maybe_recover_stale_pending_tasks() -> None:
    """Lazily вызывает recover_stale_pending_tasks() с TTL-защитой.

    Используется в list_user_tasks_from_db — чтобы пользователь увидел
    cleanup ghost-задач без ожидания рестарта сервера. TTL 60 сек
    предотвращает спам БД-запросами на каждый GET /tasks.
    """
    global _last_stale_recovery_at
    now = datetime.now(timezone.utc)
    if _last_stale_recovery_at is not None:
        elapsed = (now - _last_stale_recovery_at).total_seconds()
        if elapsed < _STALE_RECOVERY_TTL_SECONDS:
            return
    try:
        await recover_stale_pending_tasks()
    except Exception as exc:
        logger.debug(f"_maybe_recover_stale_pending_tasks: {exc}")


async def save_task_final_state_from_snapshot(
    task_id: str,
    *,
    status: str,
    progress: int,
    error: Optional[str] = None,
    total_dtp: int = 0,
    total_dead: int = 0,
    total_injured: int = 0,
    files: Optional[List[Dict[str, Any]]] = None,
    analytics: Optional[Dict[str, Any]] = None,
) -> bool:
    """Обновляет финальное состояние задачи в БД напрямую из snapshot-полей.

    Используется Celery-воркером после завершения pipeline (DONE или FAILED),
    чтобы БД содержала корректный статус — даже если Redis snapshot
    потом протухнет или будет потерян. Это предотвращает появление
    ghost-задач в будущем (см. recover_stale_pending_tasks выше).

    В отличие от save_task(), НЕ требует полного Task-объекта — принимает
    плоские поля, которые воркер уже имеет в snapshot.

    Returns:
        True если обновление прошло успешно, False иначе.
    """
    if not is_db_ready():
        return False
    pool = get_pool()
    if pool is None:
        return False

    try:
        async with pool.connection() as conn:
            await conn.execute(
                """
                UPDATE tasks
                SET status = %s,
                    progress = %s,
                    error = %s,
                    total_dtp = %s,
                    total_dead = %s,
                    total_injured = %s,
                    files = COALESCE(%s, files),
                    analytics = COALESCE(%s, analytics),
                    updated_at = NOW()
                WHERE id = %s
                """,
                params=[
                    status,
                    progress,
                    error,
                    int(total_dtp or 0),
                    int(total_dead or 0),
                    int(total_injured or 0),
                    Jsonb(files) if files is not None else None,
                    Jsonb(analytics) if analytics is not None else None,
                    task_id,
                ],
            )
            await conn.commit()
        return True
    except Exception as exc:
        logger.warning(
            f"save_task_final_state_from_snapshot({task_id}) failed: {exc}"
        )
        return False


def save_task_final_state_from_snapshot_sync(
    task_id: str,
    *,
    status: str,
    progress: int,
    error: Optional[str] = None,
    total_dtp: int = 0,
    total_dead: int = 0,
    total_injured: int = 0,
    files: Optional[List[Dict[str, Any]]] = None,
    analytics: Optional[Dict[str, Any]] = None,
) -> bool:
    """Синхронная версия save_task_final_state_from_snapshot для Celery-воркера.

    Celery-воркер работает в синхронном контексте и не имеет доступа к
    async-пулу FastAPI процесса. Эта функция открывает свежее sync-соединение
    напрямую через DATABASE_URL, выполняет UPDATE, закрывает соединение.

    Используется редко (1 раз на завершение pipeline), так что отсутствие
    пула ок — overhead на conn-open пренебрежимо мал по сравнению с
    временем pipeline (10-60 сек).

    Если DATABASE_URL не задан или БД недоступна — возвращает False
    (silent fallback, как и в остальных repository-функциях).

    Returns:
        True если обновление прошло успешно, False иначе.
    """
    try:
        from ..config import settings
    except Exception:
        return False
    if not settings.db_enabled or not settings.database_url:
        return False

    try:
        import psycopg
    except Exception as exc:
        logger.debug(
            f"save_task_final_state_from_snapshot_sync({task_id}): "
            f"psycopg not available: {exc}"
        )
        return False

    try:
        with psycopg.connect(settings.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE tasks
                    SET status = %s,
                        progress = %s,
                        error = %s,
                        total_dtp = %s,
                        total_dead = %s,
                        total_injured = %s,
                        files = COALESCE(%s, files),
                        analytics = COALESCE(%s, analytics),
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (
                        status,
                        progress,
                        error,
                        int(total_dtp or 0),
                        int(total_dead or 0),
                        int(total_injured or 0),
                        Jsonb(files) if files is not None else None,
                        Jsonb(analytics) if analytics is not None else None,
                        task_id,
                    ),
                )
            conn.commit()
        return True
    except Exception as exc:
        logger.warning(
            f"save_task_final_state_from_snapshot_sync({task_id}) failed: {exc}"
        )
        return False


# ====================================================================
# Sprint 6: Сохранение LLM-сессий (summary + qa_history)
# ====================================================================
# Раньше task.llm_summary_state и task.llm_qa_history были чисто
# in-memory — после рестарта приложения пользователь терял всё:
# резюме (нужно было перегенерировать) и Q&A-историю (массив пустой).
# Sprint 6: персистим в таблице llm_sessions и восстанавливаем
# при первом обращении через get_task_async().
#
# Три функции:
#   - save_llm_session: upsert — сохраняет summary (полная перезапись).
#   - append_qa_entry: atomic jsonb insert — добавляет один Q&A в конец
#     массива qa_history, тримит до 10 последних. НЕ трогает summary.
#   - load_llm_session: возвращает dict {summary_text, summary_provider,
#     summary_generated_at, qa_history} или None. Вызывается при
#     восстановлении задачи в get_task_async.
# ====================================================================


async def save_llm_session(
    task_id: str,
    user_id: int,
    summary_text: str,
    summary_provider: str,
    summary_generated_at: Optional[datetime] = None,
    qa_history: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """
    Sprint 6: upsert LLM-сессии в БД.

    Сохраняет summary-текст (перезаписывает, если уже было). qa_history
    обновляется только если передан явно (для полного восстановления
    при save_task — обычно нет, т.к. append_qa_entry добавляет по одной).

    Если БД недоступна — тихо пропускает (in-memory fallback: сессия
    всё равно потеряется при рестарте, но текущая работа пользователя
    не должна обрываться).
    """
    if not is_db_ready():
        return

    pool = get_pool()
    if pool is None:
        return

    if summary_generated_at is None:
        summary_generated_at = datetime.now(timezone.utc)

    try:
        async with pool.connection() as conn:
            # qa_history — опциональный, COALESCE сохраняет существующий.
            # Используем Jsonb (а не Json) — колонка qa_history имеет тип
            # JSONB, и только Jsonb адаптируется к JSONB без необходимости
            # явного каста. Json даёт json-тип, который при использовании в
            # бинарных операторах (jsonb || json) падает с ошибкой
            # "operator does not exist: jsonb || json".
            qa_json = Jsonb(qa_history) if qa_history is not None else None
            await conn.execute(
                """
                INSERT INTO llm_sessions (
                    task_id, user_id,
                    summary_text, summary_provider, summary_generated_at,
                    qa_history, updated_at
                ) VALUES (
                    %(tid)s, %(uid)s,
                    %(st)s, %(sp)s, %(sgt)s,
                    COALESCE(%(qh)s, '[]'::jsonb), NOW()
                )
                ON CONFLICT (task_id) DO UPDATE SET
                    user_id = EXCLUDED.user_id,
                    summary_text = EXCLUDED.summary_text,
                    summary_provider = EXCLUDED.summary_provider,
                    summary_generated_at = EXCLUDED.summary_generated_at,
                    qa_history = COALESCE(%(qh)s, llm_sessions.qa_history),
                    updated_at = NOW()
                """,
                params={
                    "tid": task_id,
                    "uid": user_id,
                    "st": summary_text,
                    "sp": summary_provider,
                    "sgt": summary_generated_at,
                    "qh": qa_json,
                },
            )
            await conn.commit()
        logger.info(
            f"Sprint 6: saved LLM session for task={task_id} "
            f"(summary {len(summary_text)} chars, provider={summary_provider})"
        )
    except Exception as exc:
        logger.warning(
            f"Sprint 6: save_llm_session({task_id}) failed: {exc}"
        )


async def append_qa_entry(
    task_id: str,
    user_id: int,
    question: str,
    answer: str,
    provider: str,
    timestamp: Optional[datetime] = None,
) -> None:
    """
    Sprint 6: atomic append Q&A-записи в llm_sessions.qa_history JSONB.

    Использует jsonb_insert для добавления в конец массива, затем
    тримит до 10 последних (по аналогии с task.llm_qa_history logic).

    summary НЕ трогает — он сохраняется отдельно через save_llm_session.

    Если записи для task_id ещё нет — создаёт с пустым summary и одним
    Q&A. Это нормально: summary будет сохранён позже, либо вообще не
    был сгенерирован (пользователь сразу пошёл в Q&A).
    """
    if not is_db_ready():
        return

    pool = get_pool()
    if pool is None:
        return

    if timestamp is None:
        timestamp = datetime.now(timezone.utc)

    new_entry = {
        "question": question,
        "answer": answer,
        "provider": provider,
        "timestamp": timestamp.isoformat() if hasattr(timestamp, "isoformat")
        else str(timestamp),
    }

    try:
        async with pool.connection() as conn:
            # Сначала upsert пустой записи (если ещё нет) — это гарантирует,
            # что INSERT ниже не упадёт по FOREIGN KEY / NOT NULL.
            await conn.execute(
                """
                INSERT INTO llm_sessions (
                    task_id, user_id, qa_history, updated_at
                ) VALUES (
                    %(tid)s, %(uid)s, '[]'::jsonb, NOW()
                )
                ON CONFLICT (task_id) DO NOTHING
                """,
                params={"tid": task_id, "uid": user_id},
            )

            # atomic append + trim до 10 последних:
            # 1. qa_history || new_entry → добавляет в конец
            # 2. CASE WHEN jsonb_array_length > 10 → берём последние 10
            #    через jsonb_path_query_array ('$[last 10 to last]')
            #
            # ВАЖНО: используем Jsonb (не Json) — колонка qa_history имеет
            # тип JSONB, и оператор `||` определён только для (jsonb, jsonb).
            # Json адаптируется к типу json, что вызывало ошибку:
            #   operator does not exist: jsonb || json
            # Дополнительно добавлен явный каст %(entry)s::jsonb для
            # надёжности (на случай если пул вернёт кэшированный prepared
            # statement с другим типом параметра).
            await conn.execute(
                """
                UPDATE llm_sessions
                SET qa_history = (
                    CASE
                        WHEN jsonb_array_length(qa_history || %(entry)s::jsonb) > 10
                        THEN (
                            SELECT jsonb_agg(elem)
                            FROM jsonb_array_elements(qa_history || %(entry)s::jsonb)
                            WITH ORDINALITY AS arr(elem, idx)
                            WHERE idx > jsonb_array_length(qa_history || %(entry)s::jsonb) - 10
                        )
                        ELSE qa_history || %(entry)s::jsonb
                    END
                ),
                user_id = %(uid)s,
                updated_at = NOW()
                WHERE task_id = %(tid)s
                """,
                params={
                    "tid": task_id,
                    "uid": user_id,
                    "entry": Jsonb(new_entry),
                },
            )
            await conn.commit()
        logger.info(
            f"Sprint 6: appended Q&A to session task={task_id} "
            f"(answer {len(answer)} chars)"
        )
    except Exception as exc:
        logger.warning(
            f"Sprint 6: append_qa_entry({task_id}) failed: {exc}"
        )


async def load_llm_session(task_id: str) -> Optional[Dict[str, Any]]:
    """
    Sprint 6: загружает LLM-сессию из БД.

    Возвращает dict:
        {
            "summary_text": str | None,
            "summary_provider": str | None,
            "summary_generated_at": datetime | None,
            "qa_history": list[dict],     # []
        }
    или None, если записи нет / БД недоступна.

    Вызывается из get_task_async() при cache-miss в in-memory, чтобы
    восстановить task.llm_summary_state и task.llm_qa_history.
    """
    if not is_db_ready():
        return None

    pool = get_pool()
    if pool is None:
        return None

    try:
        async with pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT summary_text, summary_provider, summary_generated_at,
                       qa_history
                FROM llm_sessions
                WHERE task_id = %(tid)s
                """,
                params={"tid": task_id},
                prepare=False,
            )
            row = await cur.fetchone()

        if row is None:
            return None

        return {
            "summary_text": row.get("summary_text"),
            "summary_provider": row.get("summary_provider"),
            "summary_generated_at": row.get("summary_generated_at"),
            "qa_history": list(row.get("qa_history") or []),
        }
    except Exception as exc:
        logger.warning(
            f"Sprint 6: load_llm_session({task_id}) failed: {exc}"
        )
        return None

