"""
worker/tasks/clusters_tasks.py — Celery задача для расчёта очагов (Sprint 7, Фаза C.3).

Очередь: clusters (concurrency=2 в docker-compose).

Задачи:
- clusters_calc_task — расчёт очагов концентрации ДТП.
  Оборачивает core.calculate_clusters_sync.
  Принимает ПОДГОТОВЛЕННЫЕ cards/prev_cards (не Task), возвращает готовый
  result dict, пишет прогресс в task_state (Redis).

Подготовка данных (cards, prev_cards, prev_label, cameras) — ответственность
FastAPI-стороны (через services/pipeline.ensure_cards / ensure_prev_cards
+ camera_cache). Celery получает ГОТОВЫЕ данные.

Cache:
  Перед расчётом проверяем db.clusters_cache.get_cached_clusters (через
  asyncio.run в Celery sync-контексте). При cache hit (~100 мс) — возвращаем
  результат без вызова OSM Overpass (15-30 сек). При cache miss — рассчитываем
  и сохраняем в кэш.

Backward compatibility:
  При USE_CELERY=false — dispatcher вызывает async-функцию напрямую
  services/clusters_ops.start_clusters_calculation.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from celery import Task as CeleryTask

from worker.celery_app import app
from worker.task_state import load_task_state, save_task_state

logger = logging.getLogger(__name__)


# ============================================================
# Хелпер: обновление clusters_state в snapshot
# ============================================================
def _update_clusters_state_in_snapshot(
    task_id: str,
    *,
    status: Optional[str] = None,
    progress: Optional[int] = None,
    stage: Optional[str] = None,
    result: Optional[Any] = None,
    error: Optional[str] = None,
) -> None:
    """Обновляет clusters_state в snapshot'е задачи (в Redis)."""
    snapshot = load_task_state(task_id)
    if snapshot is None:
        return

    state = snapshot.get("clusters_state") or {}
    if status is not None:
        state["status"] = status
    if progress is not None:
        state["progress"] = progress
    if stage is not None:
        state["stage"] = stage
    if result is not None:
        state["result"] = result
    if error is not None:
        state["error"] = error
    if status == "running" and not state.get("started_at"):
        state["started_at"] = datetime.now(timezone.utc).isoformat()
    if status in ("done", "failed"):
        state["finished_at"] = datetime.now(timezone.utc).isoformat()

    snapshot["clusters_state"] = state
    snapshot["updated_at"] = datetime.now(timezone.utc).isoformat()

    class _TaskStub:
        pass
    stub = _TaskStub()
    for key, value in snapshot.items():
        setattr(stub, key, value)
    save_task_state(stub)


# ============================================================
# Cache helpers (async → sync через asyncio.run)
# ============================================================
def _check_clusters_cache(
    reg_code: str,
    dat_list: List[str],
    prev_dat_list: Optional[List[str]],
) -> Optional[Dict[str, Any]]:
    """Проверяет кэш очагов в PostgreSQL (через asyncio.run).

    Returns:
        dict с ключами {result, raw_clusters, raw_preclusters} при cache hit,
        None при cache miss / недоступности БД.
    """
    async def _check():
        from miniapp.backend.db.clusters_cache import get_cached_clusters
        return await get_cached_clusters(
            reg_code=reg_code,
            current_dat_list=dat_list,
            prev_dat_list=prev_dat_list,
        )

    try:
        return asyncio.run(_check())
    except Exception as exc:
        logger.warning(f"clusters_calc_task: cache lookup failed: {exc}")
        return None


def _put_clusters_cache(
    reg_code: str,
    dat_list: List[str],
    prev_dat_list: Optional[List[str]],
    result: Dict[str, Any],
    raw_clusters: Optional[List[Dict[str, Any]]] = None,
    raw_preclusters: Optional[List[Dict[str, Any]]] = None,
    region_name: str = "",
    period_label: str = "",
    prev_label: Optional[str] = None,
) -> None:
    """Сохраняет результат в кэш очагов (через asyncio.run)."""
    async def _put():
        from miniapp.backend.db.clusters_cache import put_cached_clusters
        await put_cached_clusters(
            reg_code=reg_code,
            current_dat_list=dat_list,
            prev_dat_list=prev_dat_list,
            result=result,
            raw_clusters=raw_clusters,
            raw_preclusters=raw_preclusters,
            region_name=region_name,
            current_period_label=period_label,
            prev_period_label=prev_label,
        )

    try:
        asyncio.run(_put())
    except Exception as exc:
        logger.warning(f"clusters_calc_task: cache put failed: {exc}")


# ============================================================
# clusters_calc_task
# ============================================================
@app.task(
    name="worker.tasks.clusters_tasks.clusters_calc_task",
    queue="clusters",
    bind=True,
    base=CeleryTask,
    max_retries=0,  # OSM Overpass обычно_transient, но ретраи не помогают (таймаут)
    acks_late=True,
)
def clusters_calc_task(
    self: CeleryTask,
    task_id: str,
    cards: List[Dict[str, Any]],
    prev_cards: Optional[List[Dict[str, Any]]] = None,
    prev_label: Optional[str] = None,
    reg_code: Optional[str] = None,
    region_name: str = "",
    current_label: str = "",
    dat_list: Optional[List[str]] = None,
    prev_dat_list: Optional[List[str]] = None,
    use_cache: bool = True,
) -> Dict[str, Any]:
    """Рассчитывает очаги концентрации ДТП.

    Args:
        task_id: ID задачи (для обновления state в Redis).
        cards: Карточки ДТП текущего периода.
        prev_cards: Карточки ДТП прошлого периода (для динамики АППГ).
        prev_label: Метка прошлого периода (например "2024 год").
        reg_code: Код региона (для OSM Overpass).
        region_name: Название региона.
        current_label: Метка текущего периода.
        dat_list: Список периодов текущего года (для кэша).
        prev_dat_list: Список периодов прошлого года (для кэша).
        use_cache: True → проверять/сохранять кэш в PostgreSQL.

    Returns:
        dict:
        {
            "ok": bool,
            "task_id": str,
            "result": dict | None,   # clusters_state.result
            "error": str | None,
        }
    """
    log_prefix = f"Celery[clusters_calc_task:{task_id}]"

    from miniapp.backend.core import calculate_clusters_sync

    logger.info(
        f"{log_prefix}: started — cards={len(cards)}, "
        f"prev_cards={len(prev_cards or [])}, region={reg_code}"
    )

    _update_clusters_state_in_snapshot(
        task_id, status="running", progress=10, stage="Подготовка данных..."
    )

    # === Cache lookup ===
    if use_cache and reg_code and dat_list:
        cached = _check_clusters_cache(reg_code, dat_list, prev_dat_list)
        if cached is not None:
            cached_result = cached.get("result")
            cached_raw_clusters = cached.get("raw_clusters")
            cached_raw_preclusters = cached.get("raw_preclusters")
            if cached_result and (cached_raw_clusters or cached_raw_preclusters):
                _update_clusters_state_in_snapshot(
                    task_id,
                    status="done",
                    progress=100,
                    stage="Готово (из кэша)",
                    result=cached_result,
                )
                logger.info(
                    f"{log_prefix}: cache HIT — "
                    f"{cached_result.get('total_clusters', 0)} очагов"
                )
                return {
                    "ok": True,
                    "task_id": task_id,
                    "result": cached_result,
                    "from_cache": True,
                    "error": None,
                }
            else:
                logger.info(
                    f"{log_prefix}: cache hit но raw=None (старая запись) — "
                    f"пересчитываем"
                )
        else:
            logger.info(f"{log_prefix}: cache miss — считаем")

    _update_clusters_state_in_snapshot(
        task_id, progress=20, stage="Загрузка границ НП из OpenStreetMap..."
    )

    # === Загружаем камеры (для enrichment) ===
    cameras: Optional[List[Dict[str, Any]]] = None
    if reg_code:
        try:
            import sys
            from pathlib import Path
            _PROJECT_ROOT = Path(__file__).resolve().parents[2]
            if str(_PROJECT_ROOT) not in sys.path:
                sys.path.insert(0, str(_PROJECT_ROOT))
            camera_cache = __import__("camera_cache")
            if camera_cache.has_cached_cameras(reg_code):
                cameras = camera_cache.load_cameras_from_cache(reg_code)
                logger.info(
                    f"{log_prefix}: loaded {len(cameras)} cameras for enrichment"
                )
        except Exception as exc:
            logger.warning(f"{log_prefix}: camera load failed: {exc}")
            cameras = None

    _update_clusters_state_in_snapshot(
        task_id, progress=50, stage="Кластеризация..."
    )

    # === Расчёт ===
    try:
        result = calculate_clusters_sync(
            cards=cards,
            prev_cards=prev_cards,
            prev_label=prev_label,
            reg_code=reg_code,
            region_name=region_name,
            current_label=current_label,
            cameras=cameras,
            log_prefix=log_prefix,
        )
    except Exception as exc:
        logger.exception(f"{log_prefix}: failed")
        _update_clusters_state_in_snapshot(
            task_id, status="failed", progress=0, error=str(exc)
        )
        return {
            "ok": False,
            "task_id": task_id,
            "result": None,
            "from_cache": False,
            "error": str(exc),
        }

    _update_clusters_state_in_snapshot(
        task_id,
        status="done",
        progress=100,
        stage="Готово",
        result=result,
    )

    # === Cache put ===
    if use_cache and reg_code and dat_list:
        # raw_clusters не включены в result (слишком тяжёлые для JSON).
        # В Фазе C.4 можно сохранить их отдельно (S3 / PostgreSQL large object).
        _put_clusters_cache(
            reg_code=reg_code,
            dat_list=dat_list,
            prev_dat_list=prev_dat_list,
            result=result,
            raw_clusters=None,
            raw_preclusters=None,
            region_name=region_name,
            period_label=current_label,
            prev_label=prev_label,
        )

    logger.info(
        f"{log_prefix}: DONE — "
        f"{result.get('total_clusters', 0)} очагов, "
        f"{result.get('total_preclusters', 0)} предочагов"
    )

    return {
        "ok": True,
        "task_id": task_id,
        "result": result,
        "from_cache": False,
        "error": None,
    }
