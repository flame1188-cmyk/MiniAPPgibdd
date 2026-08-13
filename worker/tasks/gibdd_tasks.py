"""
worker/tasks/gibdd_tasks.py — Celery задачи для выгрузки карточек ДТП (Sprint 7, Фаза C.3).

Очередь: gibdd (concurrency=3 в docker-compose).

Задачи:
- execute_pipeline_task — полный пайплайн FETCHING → PARSING → ANALYTICS → GENERATING → DONE
  Реализован как ОДИН Celery task, вызывающий step_fetch / step_parse / step_analytics /
  step_export последовательно. Промежуточные результаты НЕ сохраняются в Redis между
  шагами (это был бы chain из 4 задач с состоянием в Redis — Фаза C.4).

  Причины единого task:
  1. Серийные шаги внутри одного task — меньше overhead на Celery (send/ack/receive).
  2. cards (3-12 MB) слишком тяжёлые для JSON-сериализации между task'ами.
  3. Restartability на уровне шага обеспечивается через cards_cache (PostgreSQL):
     если task упал на GENERATING — повторный запуск найдёт cards в кэше и пропустит FETCHING.

  Прогресс пишется в task_state (Redis) после каждого шага — FastAPI router
  читает его для polling.

- fetch_cards_task — отдельная задача для лёгкой выгрузки (без pipeline).
  Используется для pre-fetch (например, для предзагрузки популярных регионов).

Backward compatibility:
  При USE_CELERY=false (или без Redis) — dispatcher использует asyncio.create_task
  в FastAPI event loop (legacy path через services/pipeline.execute_task).
  См. worker/dispatcher.py.
"""
from __future__ import annotations

import base64
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from celery import Task as CeleryTask

from worker.celery_app import app
from worker.task_state import save_task_state_dict, load_task_state

# Lazy imports — чтобы Celery worker не падал при импорте, если
# miniapp.backend.core ещё не на sys.path (см. celery_app.py:sys.path.insert)
_PROJECT_ROOT = Path(__file__).resolve().parents[2]

logger = logging.getLogger(__name__)


# ============================================================
# Хелперы для записи файлов (используются execute_pipeline_task)
# ============================================================
def _task_dir(task_id: str) -> Path:
    """Возвращает директорию для файлов задачи (data/tasks/{task_id}/).

    Создаёт директорию при необходимости. Celery worker и FastAPI
    разделяют volume (см. docker-compose.yml: tasks_data:/tmp/gibdd_tasks),
    поэтому файлы видны обоим процессам.
    """
    # На bothost: /app/data/tasks/{task_id}
    # В docker-compose: /tmp/gibdd_tasks/{task_id} (shared volume)
    # В локальной разработке: gibdd-bot/data/tasks/{task_id}
    base = _PROJECT_ROOT / "data" / "tasks"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except Exception:
        # Fallback на /tmp — может не быть прав на запись в data/
        base = Path("/tmp") / "gibdd_tasks"
        base.mkdir(parents=True, exist_ok=True)
    d = base / task_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sanitize_filename(s: str, max_len: int = 30) -> str:
    """Делает строку безопасной для использования в имени файла."""
    out = "".join(c if c.isalnum() else "_" for c in s)[:max_len]
    return out or "unknown"


def _init_snapshot(
    task_id: str,
    *,
    dat_list: List[str],
    reg_code: str,
    region_name: str,
    period_label: str,
    user_id: int = 0,
    raw_query: str = "",
    prev_label: Optional[str] = None,
) -> None:
    """Создаёт полный начальный snapshot в Redis, если его ещё нет.

    Вызывается ОДИН раз в начале execute_pipeline_task. Если snapshot уже
    существует (например, FastAPI-сторона успела его создать через
    save_task_state(real_task)) — ничего не делает.

    Это гарантирует, что в Redis есть ВСЕ поля (user_id, region_code, ...),
    которые потом читает _maybe_merge_redis_snapshot на API-стороне.
    """
    existing = load_task_state(task_id)
    if existing is not None:
        # Snapshot уже есть — не перезаписываем (FastAPI мог сохранить больше)
        return

    now = datetime.now(timezone.utc).isoformat()
    snapshot = {
        "id": task_id,
        "user_id": user_id,
        "region_code": reg_code,
        "region_name": region_name,
        "period_label": period_label,
        "dat_list": list(dat_list or []),
        "raw_query": raw_query or "",
        "prev_label": prev_label or "",
        "status": "pending",
        "progress": 0,
        "error": None,
        "files": [],
        "analytics": None,
        "total_dtp": 0,
        "total_dead": 0,
        "total_injured": 0,
        "created_at": now,
        "updated_at": now,
        "llm_summary_state": {},
        "clusters_state": {},
        "_source": "celery_v1_init",
    }
    save_task_state_dict(task_id, snapshot)


def _update_snapshot(
    task_id: str,
    *,
    status: Optional[str] = None,
    progress: Optional[int] = None,
    error: Optional[str] = None,
    total_dtp: Optional[int] = None,
    total_dead: Optional[int] = None,
    total_injured: Optional[int] = None,
    files: Optional[List[Dict[str, Any]]] = None,
    analytics: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Обновляет snapshot task_state в Redis.

    Загружает существующий snapshot, применяет указанные поля, сохраняет
    напрямую через save_task_state_dict (БЕЗ конвертации dict → stub → dict,
    которая раньше падала на отсутствующих атрибутах _TaskStub).

    Возвращает обновлённый snapshot или None если Redis недоступен.
    """
    snapshot = load_task_state(task_id)
    if snapshot is None:
        # Snapshot ещё не создан — создаём минимальный.
        # Это может случиться только если _init_snapshot не был вызван
        # (например, в тестах). В проде _init_snapshot вызывается первым.
        snapshot = {
            "id": task_id,
            "user_id": 0,
            "region_code": "",
            "region_name": "",
            "period_label": "",
            "dat_list": [],
            "raw_query": "",
            "status": "pending",
            "progress": 0,
            "error": None,
            "files": [],
            "analytics": None,
            "total_dtp": 0,
            "total_dead": 0,
            "total_injured": 0,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "llm_summary_state": {},
            "clusters_state": {},
        }

    if status is not None:
        snapshot["status"] = status
    if progress is not None:
        snapshot["progress"] = progress
    if error is not None:
        snapshot["error"] = error
    if total_dtp is not None:
        snapshot["total_dtp"] = total_dtp
    if total_dead is not None:
        snapshot["total_dead"] = total_dead
    if total_injured is not None:
        snapshot["total_injured"] = total_injured
    if files is not None:
        snapshot["files"] = files
    if analytics is not None:
        snapshot["analytics"] = analytics

    # save_task_state_dict сам обновит updated_at
    save_task_state_dict(task_id, snapshot)
    return snapshot


# ============================================================
# Главная задача: execute_pipeline_task
# ============================================================
@app.task(
    name="worker.tasks.gibdd_tasks.execute_pipeline_task",
    queue="gibdd",
    bind=True,
    base=CeleryTask,
    max_retries=0,  # не ретраим — pipeline длинный, лучше пересоздать задачу
    acks_late=True,
)
def execute_pipeline_task(
    self: CeleryTask,
    task_id: str,
    dat_list: List[str],
    reg_code: str,
    region_name: str,
    period_label: str,
    prev_dat_list: Optional[List[str]] = None,
    prev_label: Optional[str] = None,
    user_id: int = 0,
    raw_query: str = "",
) -> Dict[str, Any]:
    """Полный пайплайн выгрузки ДТП (FETCHING → PARSING → ANALYTICS → GENERATING → DONE).

    Args:
        task_id: ID задачи (создан FastAPI-стороной через create_task).
        dat_list: Список периодов текущего года в формате ["1.2026", "2.2026", ...].
        reg_code: Код региона (например "1141").
        region_name: Название региона (например "Ленинградская область").
        period_label: Метка периода для UI (например "7 мес. 2026").
        prev_dat_list: Список периодов прошлого года (для АППГ).
                       Если None — вычисляется из dat_list (год - 1).
        prev_label: Метка прошлого периода (например "7 мес. 2025").
        user_id: Telegram user_id (для аудит-лога, опционально).
        raw_query: Сырой запрос пользователя (для аудит-лога, опционально).

    Returns:
        dict:
        {
            "ok": bool,
            "task_id": str,
            "status": "done"|"failed",
            "total_dtp": int,
            "total_dead": int,
            "total_injured": int,
            "files": list[dict],   # metadata файлов
            "error": str | None,
        }

    Side effects:
        - Обновляет task_state в Redis на каждом шаге (status, progress).
        - Записывает файлы в data/tasks/{task_id}/:
          dtp_cards_{region}_{period}.xlsx
          dtp_uch_{region}_{period}.xlsx
          dtp_map_{region}_{period}.html
        - Логирует каждый шаг с префиксом "Celery[execute_pipeline_task]".
    """
    log_prefix = f"Celery[execute_pipeline_task:{task_id}]"

    # Импортируем core/ функции (lazy — celery_app.py уже добавил _PROJECT_ROOT в sys.path)
    from miniapp.backend.core.pipeline_steps import (
        step_analytics,
        step_export,
        step_fetch,
        step_parse,
    )

    logger.info(
        f"{log_prefix}: started — region={reg_code} ({region_name}), "
        f"period={period_label}, dat_list={dat_list}"
    )

    # Если prev_dat_list не задан — вычисляем
    if not prev_dat_list:
        prev_dat_list = []
        for dat in dat_list:
            try:
                m, y = dat.split(".")
                prev_dat_list.append(f"{m}.{int(y) - 1}")
            except Exception:
                continue
    if not prev_label:
        try:
            year = int(dat_list[0].split(".")[1]) if dat_list else 0
            if year:
                prev_label = period_label.replace(str(year), str(year - 1))
            else:
                prev_label = "Прошлый период"
        except Exception:
            prev_label = "Прошлый период"

    # === Шаг 0: Инициализация snapshot в Redis (если ещё нет) ===
    # Важно: это должно происходить ДО первого _update_snapshot, иначе
    # _update_snapshot создаст минимальный snapshot без user_id/region_code/...
    # и API-сторона не сможет смержить корректные поля.
    _init_snapshot(
        task_id,
        dat_list=dat_list,
        reg_code=reg_code,
        region_name=region_name,
        period_label=period_label,
        user_id=user_id,
        raw_query=raw_query,
        prev_label=prev_label,
    )

    # === Шаг 1: FETCHING (current period) ===
    _update_snapshot(task_id, status="fetching", progress=10)
    fetch_result = step_fetch(
        dat_list=dat_list,
        reg_code=reg_code,
        log_prefix=f"{log_prefix}/fetch",
    )
    if not fetch_result["ok"]:
        err = fetch_result.get("error", "Fetch failed")
        _update_snapshot(task_id, status="failed", progress=10, error=err)
        logger.error(f"{log_prefix}: FETCHING failed — {err}")
        return {
            "ok": False,
            "task_id": task_id,
            "status": "failed",
            "total_dtp": 0,
            "total_dead": 0,
            "total_injured": 0,
            "files": [],
            "error": err,
        }

    cards = fetch_result["cards"]
    stats = fetch_result["stats"]
    logger.info(
        f"{log_prefix}: FETCHING done — {stats['total_dtp']} ДТП, "
        f"{stats['total_dead']} погибших, {stats['total_injured']} раненых"
    )
    _update_snapshot(
        task_id,
        status="fetching",
        progress=20,
        total_dtp=stats["total_dtp"],
        total_dead=stats["total_dead"],
        total_injured=stats["total_injured"],
    )

    # === Шаг 1b: FETCHING prev period (для АППГ) ===
    prev_cards: List[Dict[str, Any]] = []
    if prev_dat_list:
        prev_fetch = step_fetch(
            dat_list=prev_dat_list,
            reg_code=reg_code,
            log_prefix=f"{log_prefix}/fetch_prev",
        )
        if prev_fetch["ok"]:
            prev_cards = prev_fetch["cards"]
            logger.info(
                f"{log_prefix}: prev FETCHING done — {len(prev_cards)} ДТП "
                f"({prev_label})"
            )
        else:
            logger.warning(
                f"{log_prefix}: prev FETCHING failed — {prev_fetch.get('error')} "
                f"— analytics without comparison"
            )

    # === Шаг 2: PARSING ===
    _update_snapshot(task_id, status="parsing", progress=45)
    parse_result = step_parse(cards, log_prefix=f"{log_prefix}/parse")
    if not parse_result["ok"]:
        err = parse_result.get("error", "Parse failed")
        _update_snapshot(task_id, status="failed", progress=45, error=err)
        logger.error(f"{log_prefix}: PARSING failed — {err}")
        return {
            "ok": False,
            "task_id": task_id,
            "status": "failed",
            "total_dtp": stats["total_dtp"],
            "total_dead": stats["total_dead"],
            "total_injured": stats["total_injured"],
            "files": [],
            "error": err,
        }

    file1_data = parse_result["file1_data"]
    file2_data = parse_result["file2_data"]
    logger.info(
        f"{log_prefix}: PARSING done — file1={len(file1_data)} строк, "
        f"file2={len(file2_data)} строк"
    )

    # === Шаг 3: ANALYTICS ===
    _update_snapshot(task_id, status="analytics", progress=65)
    analytics_result = step_analytics(
        cards=cards,
        prev_cards=prev_cards or None,
        prev_label=prev_label,
        current_label=period_label,
        log_prefix=f"{log_prefix}/analytics",
    )
    if not analytics_result["ok"]:
        # Analytics не critical — продолжаем с пустым analytics
        logger.warning(
            f"{log_prefix}: ANALYTICS failed — {analytics_result.get('error')} "
            f"— продолжаем без аналитики"
        )
        analytics_dict = {
            "total_dtp": stats["total_dtp"],
            "total_dead": stats["total_dead"],
            "total_injured": stats["total_injured"],
            "has_prev_data": bool(prev_cards),
        }
    else:
        analytics_dict = analytics_result["analytics"]

    _update_snapshot(task_id, status="analytics", progress=75, analytics=analytics_dict)

    # === Шаг 4: GENERATING (Excel + HTML map) ===
    _update_snapshot(task_id, status="generating", progress=80)

    # Загружаем камеры из кэша (если есть)
    cameras: Optional[List[Dict[str, Any]]] = None
    try:
        import sys
        if str(_PROJECT_ROOT) not in sys.path:
            sys.path.insert(0, str(_PROJECT_ROOT))
        camera_cache = __import__("camera_cache")
        if camera_cache.has_cached_cameras(reg_code):
            cameras = camera_cache.load_cameras_from_cache(reg_code)
            logger.info(
                f"{log_prefix}: loaded {len(cameras)} cameras for region {reg_code}"
            )
    except Exception as exc:
        logger.warning(f"{log_prefix}: camera cache load failed: {exc}")
        cameras = None

    export_result = step_export(
        file1_data=file1_data,
        file2_data=file2_data,
        cards=cards,
        region_name=region_name,
        period_label=period_label,
        cameras=cameras,
        prev_cards=prev_cards or None,
        prev_label=prev_label,
        log_prefix=f"{log_prefix}/export",
    )
    if not export_result["ok"]:
        err = export_result.get("error", "Export failed")
        _update_snapshot(task_id, status="failed", progress=80, error=err)
        logger.error(f"{log_prefix}: GENERATING failed — {err}")
        return {
            "ok": False,
            "task_id": task_id,
            "status": "failed",
            "total_dtp": stats["total_dtp"],
            "total_dead": stats["total_dead"],
            "total_injured": stats["total_injured"],
            "files": [],
            "error": err,
        }

    # === Записываем файлы на диск ===
    out_dir = _task_dir(task_id)
    region_safe = _sanitize_filename(region_name, 30)
    period_safe = _sanitize_filename(period_label, 20)

    files_meta: List[Dict[str, Any]] = []

    # file1: dtp_cards
    file1_bytes = base64.b64decode(export_result["file1_bytes_b64"])
    file1_name = f"dtp_cards_{region_safe}_{period_safe}.xlsx"
    file1_path = out_dir / file1_name
    file1_path.write_bytes(file1_bytes)
    files_meta.append({
        "type": "dtp_cards",
        "filename": file1_name,
        "path": str(file1_path),
        "size_bytes": len(file1_bytes),
        "mime": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    })

    # file2: dtp_participants
    file2_bytes = base64.b64decode(export_result["file2_bytes_b64"])
    file2_name = f"dtp_uch_{region_safe}_{period_safe}.xlsx"
    file2_path = out_dir / file2_name
    file2_path.write_bytes(file2_bytes)
    files_meta.append({
        "type": "dtp_participants",
        "filename": file2_name,
        "path": str(file2_path),
        "size_bytes": len(file2_bytes),
        "mime": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    })

    # file3: HTML map (опционально)
    map_html = export_result.get("map_html") or ""
    if map_html:
        map_name = f"dtp_map_{region_safe}_{period_safe}.html"
        map_path = out_dir / map_name
        map_path.write_text(map_html, encoding="utf-8")
        files_meta.append({
            "type": "map_html",
            "filename": map_name,
            "path": str(map_path),
            "size_bytes": len(map_html.encode("utf-8")),
            "mime": "text/html",
        })

    logger.info(
        f"{log_prefix}: GENERATING done — file1={len(file1_bytes) // 1024} KB, "
        f"file2={len(file2_bytes) // 1024} KB, "
        f"map={len(map_html) // 1024} KB"
    )

    # === DONE ===
    _update_snapshot(
        task_id,
        status="done",
        progress=100,
        files=files_meta,
        analytics=analytics_dict,
    )

    logger.info(
        f"{log_prefix}: DONE — {stats['total_dtp']} ДТП, "
        f"{stats['total_dead']} погибших, {stats['total_injured']} раненых, "
        f"{len(files_meta)} файлов"
    )

    return {
        "ok": True,
        "task_id": task_id,
        "status": "done",
        "total_dtp": stats["total_dtp"],
        "total_dead": stats["total_dead"],
        "total_injured": stats["total_injured"],
        "files": files_meta,
        "error": None,
    }


# ============================================================
# Лёгкая задача: fetch_cards_task (только выгрузка, без pipeline)
# ============================================================
@app.task(
    name="worker.tasks.gibdd_tasks.fetch_cards_task",
    queue="gibdd",
    bind=True,
    base=CeleryTask,
    max_retries=2,
    acks_late=True,
)
def fetch_cards_task(
    self: CeleryTask,
    dat_list: List[str],
    reg_code: str,
    log_prefix: str = "Celery[fetch_cards_task]",
) -> Dict[str, Any]:
    """Только выгрузка карточек (без парсинга/аналитики/экспорта).

    Используется для:
    - Pre-fetch популярных регионов
    - Восстановления cards в задаче после рестарта
    - Тестирования выгрузки без запуска всего pipeline

    Returns:
        dict:
        {
            "ok": bool,
            "total_dtp": int,
            "total_dead": int,
            "total_injured": int,
            "error": str | None,
        }
    """
    from miniapp.backend.core.pipeline_steps import step_fetch

    result = step_fetch(
        dat_list=dat_list,
        reg_code=reg_code,
        log_prefix=log_prefix,
    )

    if not result["ok"]:
        # Retry на transient errors (API ГИБДД 502/503)
        raise self.retry(
            exc=Exception(result.get("error", "Fetch failed")),
            countdown=30,  # 30 сек между ретраями
        )

    return {
        "ok": True,
        "total_dtp": result["stats"]["total_dtp"],
        "total_dead": result["stats"]["total_dead"],
        "total_injured": result["stats"]["total_injured"],
        "error": None,
    }
