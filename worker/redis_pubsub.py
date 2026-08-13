"""
worker/redis_pubsub.py — pub/sub для streaming LLM токенов (Sprint 7, Фаза C.3).

Назначение:
  LLM-стриминг (get_ai_answer_stream) отдаёт токены по одному через async
  generator. В single-process деплое FastAPI пробрасывает их в SSE-ответ
  напрямую (await + yield).

  В multi-process деплое (Celery worker + FastAPI) — стриминг идёт в Celery
  worker (там выполняется llm_qa_task), а FastAPI должен получать токены
  через Redis pub/sub и пробрасывать их в SSE клиенту.

  Этот модуль — мост между Celery worker (publisher) и FastAPI (subscriber).

Каналы:
  - "{prefix}:llm:{task_id}" — канал стрима Q&A для конкретной задачи
  - "{prefix}:llm_summary:{task_id}" — канал стрима summary (опционально)

Сообщения (JSON-строки):
  - {"type": "token", "data": "..."} — очередной токен LLM
  - {"type": "progress", "data": 50} — прогресс (%)
  - {"type": "done", "data": {"text": "..."}} — финал, полный текст
  - {"type": "error", "data": "..."} — ошибка, стрим прерывается

Dual-mode:
  - При REDIS_URL заданном — pub/sub через Redis
  - Иначе — in-memory queue (asyncio.Queue per task_id), работает только
    в single-process деплое (dev/тесты)

Пример (Celery worker):
    from worker.redis_pubsub import publish_token, publish_done

    for token in llm_stream():
        publish_token(task_id, token)
    publish_done(task_id, full_text)

Пример (FastAPI SSE):
    from worker.redis_pubsub import subscribe

    async for msg in subscribe(task_id):
        if msg["type"] == "token":
            yield f"data: {msg['data']}\\n\\n"
        elif msg["type"] == "done":
            yield "data: [DONE]\\n\\n"
            break
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator, Dict, Optional

logger = logging.getLogger(__name__)


# ============================================================
# Config helpers (ленивый импорт)
# ============================================================
def _get_redis_url() -> str:
    try:
        import config
        return getattr(config, "REDIS_URL", "") or ""
    except Exception:
        return ""


def _get_prefix() -> str:
    try:
        import config
        return getattr(config, "REDIS_PUBSUB_PREFIX", "gibdd")
    except Exception:
        return "gibdd"


def _channel_name(task_id: str, suffix: str = "llm") -> str:
    """Возвращает имя pub/sub канала."""
    return f"{_get_prefix()}:{suffix}:{task_id}"


# ============================================================
# Redis client (lazy, отдельный от task_state — decode_responses=True)
# ============================================================
_redis_client = None
_redis_client_checked = False


def _get_redis_client():
    """Возвращает Redis client (decode_responses=True) или None."""
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
            decode_responses=True,
        )
        _redis_client.ping()
        logger.info(f"[redis_pubsub] Redis connected: {url}")
    except Exception as exc:
        logger.warning(
            f"[redis_pubsub] Redis unavailable ({exc}) — "
            f"fallback to in-memory queue (single-process only)"
        )
        _redis_client = None

    return _redis_client


# ============================================================
# In-memory fallback (single-process: dev/тесты без Redis)
# ============================================================
# task_id → list of asyncio.Queue
# Один task_id может иметь несколько подписчиков (редко, но возможно —
# например, пользователь открыл две вкладки с одним Q&A).
_subscribers: Dict[str, list[asyncio.Queue]] = {}


def _get_in_memory_subscribers(task_id: str) -> list[asyncio.Queue]:
    """Возвращает список подписчиков для task_id (in-memory fallback)."""
    return _subscribers.setdefault(task_id, [])


def _publish_in_memory(task_id: str, message: Dict[str, Any]) -> None:
    """Публикует сообщение всем in-memory подписчикам."""
    for queue in _get_in_memory_subscribers(task_id):
        try:
            queue.put_nowait(message)
        except asyncio.QueueFull:
            logger.warning(
                f"[redis_pubsub] in-memory queue full for task={task_id}, "
                f"dropping message"
            )


# ============================================================
# Publisher (используется Celery worker)
# ============================================================
def _publish(task_id: str, message: Dict[str, Any], suffix: str = "llm") -> bool:
    """Публикует сообщение в канал.

    Returns:
        True если опубликовано в Redis, False если in-memory fallback.
    """
    client = _get_redis_client()
    payload = json.dumps(message, ensure_ascii=False, default=str)

    if client is None:
        _publish_in_memory(task_id, message)
        return False

    try:
        channel = _channel_name(task_id, suffix)
        client.publish(channel, payload)
        return True
    except Exception as exc:
        logger.warning(
            f"[redis_pubsub] publish({task_id}) failed: {exc} — "
            f"fallback to in-memory"
        )
        _publish_in_memory(task_id, message)
        return False


def publish_token(task_id: str, token: str, suffix: str = "llm") -> bool:
    """Публикует очередной токен LLM."""
    return _publish(task_id, {"type": "token", "data": token}, suffix)


def publish_progress(task_id: str, progress: int, suffix: str = "llm") -> bool:
    """Публикует прогресс (0-100)."""
    return _publish(task_id, {"type": "progress", "data": int(progress)}, suffix)


def publish_done(
    task_id: str,
    full_text: str = "",
    extra: Optional[Dict[str, Any]] = None,
    suffix: str = "llm",
) -> bool:
    """Публикует done-сообщение с полным текстом ответа."""
    payload: Dict[str, Any] = {"type": "done", "data": {"text": full_text}}
    if extra:
        payload["data"].update(extra)
    return _publish(task_id, payload, suffix)


def publish_error(task_id: str, error: str, suffix: str = "llm") -> bool:
    """Публикует error-сообщение (стрим прерывается)."""
    return _publish(task_id, {"type": "error", "data": error}, suffix)


# ============================================================
# Subscriber (используется FastAPI SSE endpoint)
# ============================================================
async def subscribe(
    task_id: str,
    suffix: str = "llm",
    timeout: float = 60.0,
) -> AsyncIterator[Dict[str, Any]]:
    """Async generator — выдаёт сообщения из pub/sub канала.

    Заканчивает итерацию при получении done/error или по timeout.

    Args:
        task_id: ID задачи.
        suffix: "llm" (Q&A) или "llm_summary" (summary).
        timeout: Максимальное ожидание между сообщениями (сек).

    Yields:
        dict с полями {"type": "token"|"progress"|"done"|"error", "data": ...}
    """
    client = _get_redis_client()

    # === In-memory path ===
    if client is None:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1024)
        _get_in_memory_subscribers(task_id).append(queue)
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=timeout)
                    yield msg
                    if msg.get("type") in ("done", "error"):
                        return
                except asyncio.TimeoutError:
                    return
        finally:
            try:
                _get_in_memory_subscribers(task_id).remove(queue)
                if not _get_in_memory_subscribers(task_id):
                    _subscribers.pop(task_id, None)
            except (ValueError, KeyError):
                pass
        return

    # === Redis pub/sub path ===
    channel = _channel_name(task_id, suffix)
    pubsub = client.pubsub()
    pubsub.subscribe(channel)
    try:
        # Redis pubsub в sync-режиме — обёртка в asyncio.to_thread
        loop = asyncio.get_running_loop()

        async def _get_message():
            return await loop.run_in_executor(
                None,
                lambda: pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=timeout,
                ),
            )

        while True:
            msg = await _get_message()
            if msg is None:
                # timeout — заканчиваем стрим
                return
            if not isinstance(msg, dict):
                continue
            data = msg.get("data")
            if not data:
                continue
            try:
                parsed = json.loads(data)
            except (json.JSONDecodeError, TypeError):
                continue
            yield parsed
            if parsed.get("type") in ("done", "error"):
                return
    finally:
        try:
            pubsub.unsubscribe(channel)
            pubsub.close()
        except Exception:
            pass


# ============================================================
# Health-check
# ============================================================
def healthcheck() -> Dict[str, Any]:
    """Возвращает статус pub/sub для /health/redis."""
    client = _get_redis_client()
    if client is None:
        return {
            "available": False,
            "backend": "in_memory",
            "active_channels": len(_subscribers),
            "error": "Redis not configured or unavailable",
        }

    try:
        # Список активных pub/sub каналов с нашим prefix
        prefix = _get_prefix()
        channels = client.pubsub_channels(f"{prefix}:*")
        return {
            "available": True,
            "backend": "redis",
            "active_channels": len(channels),
            "channels": channels[:20],  # первые 20 для отладки
            "error": None,
        }
    except Exception as exc:
        return {
            "available": False,
            "backend": "in_memory",
            "active_channels": 0,
            "error": str(exc),
        }
