"""
worker/task_state.py — Redis-backed task state для Celery (Sprint 7, Фаза C.3).

Назначение:
  В Sprint 6 состояние задач хранилось в OrderedDict `_tasks` (in-memory).
  Это работало в single-process деплое, но не масштабировалось:
    - FastAPI и Celery worker — разные процессы → у каждого свой `_tasks`
    - После рестарта процесса — потеря всех in-memory задач
    - LRU eviction вытеснял активные задачи

  Phase C.3 вводит Redis-backed task state:
    - Celery worker пишет прогресс/результат в Redis
    - FastAPI router читает статус из Redis (для polling)
    - TTL 24 часа — старые task state удаляются автоматически
    - Dual-mode: при отсутствии Redis — fallback на OrderedDict `_tasks`
      (обратная совместимость с dev/тестами/малыми деплоями)

Ключи в Redis:
  - "gibdd:task_state:{task_id}" → JSON-сериализованный snapshot Task
  - TTL: config.REDIS_TASK_STATE_TTL (86400 сек = 24 часа по умолчанию)

Snapshot содержит ТОЛЬКО метаданные и лёгкие поля:
  - id, user_id, region_code, region_name, period_label, dat_list, raw_query
  - status, progress, error, total_dtp/dead/injured
  - files (list of file metadata, без самих байтов)
  - analytics (dict, может быть большим, но JSON-сериализуемым)
  - llm_summary_state (status + result text, без heavy cards)
  - clusters_state (status + result)
  - created_at, updated_at

НЕ содержит (слишком тяжёлые):
  - cards, prev_cards (3-12 MB) — они в cards_cache PostgreSQL
  - raw_clusters, raw_preclusters — они в clusters_cache PostgreSQL
  - cross_tables, current_metrics — пересчитываются из cards

Workflow:
  1. FastAPI create_task → dispatcher.dispatch_execute_pipeline()
  2. dispatcher решает: Celery queue или asyncio.create_task(execute_task)
  3. В Celery path:
     a. gibdd_tasks.execute_pipeline_task загружает snapshot из Redis
     b. По мере прогресса обновляет snapshot (status=FETCHING, PARSING, ...)
     c. На финальном шаге пишет files metadata в snapshot
  4. FastAPI GET /tasks/{id}/status читает snapshot из Redis → отдаёт фронту

Backward compatibility:
  - In-memory `_tasks` OrderedDict остаётся как fallback
  - При USE_CELERY=false состояние пишется ТОЛЬКО в `_tasks` (как раньше)
  - При USE_CELERY=true — пишется в Redis И в `_tasks` (для текущего процесса)
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# ============================================================
# Ленивый импорт config (чтобы не падать при отсутствии env)
# ============================================================
def _get_redis_url() -> str:
    """Возвращает REDIS_URL или пустую строку."""
    try:
        import config
        return getattr(config, "REDIS_URL", "") or ""
    except Exception:
        return ""


def _get_redis_prefix() -> str:
    """Возвращает префикс для Redis-ключей."""
    try:
        import config
        return getattr(config, "REDIS_PUBSUB_PREFIX", "gibdd")
    except Exception:
        return "gibdd"


def _get_ttl() -> int:
    """Возвращает TTL для task state в Redis (сек)."""
    try:
        import config
        return int(getattr(config, "REDIS_TASK_STATE_TTL", 86400))
    except Exception:
        return 86400


def _is_celery_enabled() -> bool:
    """True если USE_CELERY=true И REDIS_URL задан."""
    try:
        import config
        return bool(getattr(config, "USE_CELERY", False)) and bool(
            getattr(config, "REDIS_URL", "")
        )
    except Exception:
        return False


# ============================================================
# Redis client (lazy)
# ============================================================
_redis_client = None
_redis_client_checked = False


def _get_redis_client():
    """Возвращает Redis client или None (если Redis не сконфигурирован).

    Ленивая инициализация — первое использование пытается подключиться,
    при неудаче кэширует None, чтобы не пытаться переподключаться на каждый
    вызов.
    """
    global _redis_client, _redis_client_checked
    if _redis_client_checked:
        return _redis_client

    _redis_client_checked = True
    url = _get_redis_url()
    if not url:
        return None

    try:
        import redis  # type: ignore[import-untyped]
        _redis_client = redis.from_url(
            url,
            socket_timeout=2.0,
            socket_connect_timeout=2.0,
            decode_responses=True,  # str вместо bytes
        )
        # Тестовое ping
        _redis_client.ping()
        logger.info(f"[task_state] Redis connected: {url}")
    except Exception as exc:
        logger.warning(
            f"[task_state] Redis unavailable ({exc}) — "
            f"fallback to in-memory _tasks"
        )
        _redis_client = None

    return _redis_client


# ============================================================
# Ключи
# ============================================================
def _task_state_key(task_id: str) -> str:
    """Возвращает Redis-ключ для task state."""
    return f"{_get_redis_prefix()}:task_state:{task_id}"


# ============================================================
# Snapshot сериализация
# ============================================================
def _datetime_to_iso(dt: Any) -> Optional[str]:
    """datetime → ISO-строка (или None)."""
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.isoformat()
    if isinstance(dt, str):
        return dt
    return None


def _iso_to_datetime(s: Any) -> Optional[datetime]:
    """ISO-строка → datetime (или None)."""
    if not s:
        return None
    if isinstance(s, datetime):
        return s
    try:
        # Handle both "2026-08-13T10:30:00+00:00" and "2026-08-13T10:30:00Z"
        s_str = str(s).replace("Z", "+00:00")
        return datetime.fromisoformat(s_str)
    except Exception:
        return None


def task_to_snapshot(task: Any) -> Dict[str, Any]:
    """Сериализует Task (dataclass) в JSON-совместимый dict.

    Извлекает ТОЛЬКО лёгкие поля. Тяжёлые (cards, prev_cards, raw_clusters)
    НЕ включаются — они хранятся отдельно в PostgreSQL кэшах.
    """
    # Analysis state → dict
    def _state_to_dict(state: Any) -> Dict[str, Any]:
        if state is None:
            return {}
        return {
            "status": getattr(state, "status", None).value
            if getattr(state, "status", None) is not None
            and hasattr(getattr(state, "status", None), "value")
            else None,
            "progress": getattr(state, "progress", 0),
            "stage": getattr(state, "stage", "") or "",
            "result": getattr(state, "result", None),
            "error": getattr(state, "error", None),
            "started_at": _datetime_to_iso(getattr(state, "started_at", None)),
            "finished_at": _datetime_to_iso(getattr(state, "finished_at", None)),
        }

    return {
        "id": task.id,
        "user_id": task.user_id,
        "region_code": task.region_code,
        "region_name": task.region_name,
        "period_label": task.period_label,
        "dat_list": list(task.dat_list or []),
        "raw_query": task.raw_query,
        "status": task.status.value if hasattr(task.status, "value") else str(task.status),
        "progress": task.progress,
        "error": task.error,
        "files": list(task.files or []),
        "analytics": task.analytics,
        "total_dtp": task.total_dtp,
        "total_dead": task.total_dead,
        "total_injured": task.total_injured,
        "created_at": _datetime_to_iso(task.created_at),
        "updated_at": _datetime_to_iso(task.updated_at),
        "llm_summary_state": _state_to_dict(
            getattr(task, "llm_summary_state", None)
        ),
        "clusters_state": _state_to_dict(
            getattr(task, "clusters_state", None)
        ),
        # Метка источника — для отладки
        "_source": "celery_v1",
    }


def snapshot_to_task_updates(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Возвращает dict с полями, которые можно применить к Task через setattr.

    Используется FastAPI-стороной для обновления существующего Task из Redis
    snapshot. Не возвращает тяжёлые поля (cards, prev_cards, raw_clusters) —
    они восстанавливаются через cards_cache / clusters_cache lazy.
    """
    updates: Dict[str, Any] = {}

    if "status" in snapshot:
        # Возвращаем как строку — вызовущий код должен преобразовать в Enum
        updates["status"] = snapshot["status"]
    if "progress" in snapshot:
        updates["progress"] = snapshot["progress"]
    if "error" in snapshot:
        updates["error"] = snapshot["error"]
    if "files" in snapshot:
        updates["files"] = list(snapshot["files"] or [])
    if "analytics" in snapshot:
        updates["analytics"] = snapshot["analytics"]
    if "total_dtp" in snapshot:
        updates["total_dtp"] = snapshot["total_dtp"]
    if "total_dead" in snapshot:
        updates["total_dead"] = snapshot["total_dead"]
    if "total_injured" in snapshot:
        updates["total_injured"] = snapshot["total_injured"]
    if "updated_at" in snapshot:
        updates["updated_at"] = _iso_to_datetime(snapshot["updated_at"])

    # Analysis states (только статус/прогресс/результат — не started_at/finished_at)
    for state_field in ("llm_summary_state", "clusters_state"):
        if state_field in snapshot and snapshot[state_field]:
            state_dict = snapshot[state_field]
            updates[state_field] = {
                "status": state_dict.get("status"),
                "progress": state_dict.get("progress", 0),
                "stage": state_dict.get("stage", ""),
                "result": state_dict.get("result"),
                "error": state_dict.get("error"),
                "started_at": _iso_to_datetime(state_dict.get("started_at")),
                "finished_at": _iso_to_datetime(state_dict.get("finished_at")),
            }

    return updates


# ============================================================
# Сохранение / загрузка / удаление
# ============================================================
def save_task_state(task: Any) -> bool:
    """Сохраняет snapshot Task (dataclass) в Redis.

    Принимает ПОЛНЫЙ Task-объект (с атрибутами id, user_id, region_code,
    region_name, period_label, dat_list, raw_query, status, progress, ...).
    Используется на FastAPI-стороне, где есть реальный Task.

    Returns:
        True если сохранено в Redis, False если fallback на in-memory
        (Redis недоступен или не сконфигурирован).
    """
    client = _get_redis_client()
    if client is None:
        return False

    try:
        task_id = task.id
        snapshot = task_to_snapshot(task)
        # Обновляем updated_at — snapshot всегда свежий
        snapshot["updated_at"] = datetime.now(timezone.utc).isoformat()
        key = _task_state_key(task_id)
        client.setex(key, _get_ttl(), json.dumps(snapshot, ensure_ascii=False, default=str))
        return True
    except Exception as exc:
        task_id_for_log = getattr(task, "id", "<unknown>")
        logger.warning(f"[task_state] save_task_state({task_id_for_log}) failed: {exc}")
        return False


def save_task_state_dict(task_id: str, snapshot: Dict[str, Any]) -> bool:
    """Сохраняет snapshot task_state (dict) напрямую в Redis.

    В отличие от save_task_state(), принимает уже готовый snapshot dict —
    НЕ вызывает task_to_snapshot(). Это канонический путь для Celery worker'а:
    worker загружает snapshot из Redis (load_task_state), мутирует поля,
    сохраняет обратно через save_task_state_dict().

    Args:
        task_id: ID задачи (для логов и ключа).
        snapshot: Snapshot dict (JSON-сериализуемый). Поле "id" должно
                  совпадать с task_id (или будет перезаписано).

    Returns:
        True если сохранено в Redis, False если Redis недоступен.
    """
    client = _get_redis_client()
    if client is None:
        return False

    try:
        # Гарантируем, что id в snapshot соответствует task_id
        snapshot["id"] = task_id
        # Обновляем updated_at — snapshot всегда свежий
        snapshot["updated_at"] = datetime.now(timezone.utc).isoformat()
        key = _task_state_key(task_id)
        client.setex(
            key,
            _get_ttl(),
            json.dumps(snapshot, ensure_ascii=False, default=str),
        )
        return True
    except Exception as exc:
        logger.warning(f"[task_state] save_task_state_dict({task_id}) failed: {exc}")
        return False


def load_task_state(task_id: str) -> Optional[Dict[str, Any]]:
    """Загружает snapshot Task из Redis.

    Returns:
        dict (snapshot) или None если не найдено / Redis недоступен.
    """
    client = _get_redis_client()
    if client is None:
        return None

    try:
        key = _task_state_key(task_id)
        raw = client.get(key)
        if not raw:
            return None
        return json.loads(raw)
    except Exception as exc:
        logger.warning(f"[task_state] load_task_state({task_id}) failed: {exc}")
        return None


def delete_task_state(task_id: str) -> bool:
    """Удаляет snapshot Task из Redis.

    Returns:
        True если удалено, False если не найдено / Redis недоступен.
    """
    client = _get_redis_client()
    if client is None:
        return False

    try:
        key = _task_state_key(task_id)
        deleted = client.delete(key)
        return bool(deleted)
    except Exception as exc:
        logger.warning(f"[task_state] delete_task_state({task_id}) failed: {exc}")
        return False


def list_active_task_ids(prefix: Optional[str] = None) -> list[str]:
    """Возвращает список task_id в Redis (по prefix).

    Используется flush_stale_task_states для перебора и проверки TTL.
    """
    client = _get_redis_client()
    if client is None:
        return []

    try:
        pattern = f"{_get_redis_prefix()}:task_state:*"
        if prefix:
            pattern = f"{_get_redis_prefix()}:task_state:{prefix}*"
        # scan_iter — не блокирует Redis на больших объёмах
        keys = list(client.scan_iter(match=pattern, count=200))
        # Извлекаем task_id из ключа
        prefix_len = len(f"{_get_redis_prefix()}:task_state:")
        return [k[prefix_len:] for k in keys if len(k) > prefix_len]
    except Exception as exc:
        logger.warning(f"[task_state] list_active_task_ids failed: {exc}")
        return []


def flush_stale(max_age_seconds: Optional[int] = None) -> int:
    """Удаляет протухшие task state.

    Args:
        max_age_seconds: Если задан — удаляет записи старше этого возраста
                         (по updated_at). Если None — удаляет все (flush_all).

    Returns:
        Количество удалённых записей.
    """
    client = _get_redis_client()
    if client is None:
        return 0

    if max_age_seconds is None:
        # flush_all — удаляем все task_state ключи
        try:
            ids = list_active_task_ids()
            for tid in ids:
                delete_task_state(tid)
            return len(ids)
        except Exception as exc:
            logger.warning(f"[task_state] flush_stale(all) failed: {exc}")
            return 0

    # По возрасту — проверяем updated_at в snapshot
    cutoff = datetime.now(timezone.utc).timestamp() - max_age_seconds
    deleted = 0
    try:
        ids = list_active_task_ids()
        for tid in ids:
            snap = load_task_state(tid)
            if not snap:
                continue
            updated_str = snap.get("updated_at")
            if not updated_str:
                continue
            try:
                updated_dt = datetime.fromisoformat(
                    str(updated_str).replace("Z", "+00:00")
                )
                if updated_dt.timestamp() < cutoff:
                    if delete_task_state(tid):
                        deleted += 1
            except Exception:
                continue
        return deleted
    except Exception as exc:
        logger.warning(f"[task_state] flush_stale(age={max_age_seconds}) failed: {exc}")
        return deleted


# ============================================================
# Health-check
# ============================================================
def healthcheck() -> Dict[str, Any]:
    """Возвращает статус task_state для /health/redis.

    Returns:
        {
            "available": bool,        # Redis доступен
            "backend": "redis" | "in_memory",
            "active_task_states": int, # количество ключей в Redis
            "ttl_seconds": int,
            "error": str | None,
        }
    """
    client = _get_redis_client()
    ttl = _get_ttl()
    if client is None:
        return {
            "available": False,
            "backend": "in_memory",
            "active_task_states": 0,
            "ttl_seconds": ttl,
            "error": "Redis not configured or unavailable",
        }

    try:
        ids = list_active_task_ids()
        return {
            "available": True,
            "backend": "redis",
            "active_task_states": len(ids),
            "ttl_seconds": ttl,
            "error": None,
        }
    except Exception as exc:
        return {
            "available": False,
            "backend": "in_memory",
            "active_task_states": 0,
            "ttl_seconds": ttl,
            "error": str(exc),
        }
