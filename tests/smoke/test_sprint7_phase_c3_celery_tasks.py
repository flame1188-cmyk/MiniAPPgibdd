"""
tests/smoke/test_sprint7_phase_c3_celery_tasks.py — smoke-тесты для Фазы C.3.

Проверяет:
1. Все Celery tasks зарегистрированы (gibdd/llm/clusters/exports/cleanup)
2. Каждая задача привязана к правильной очереди
3. task_state: save/load/delete/healthcheck (in-memory fallback без Redis)
4. redis_pubsub: in-memory path (publish_token → subscribe)
5. dispatcher: возвращает task_id, в in-memory режиме планирует asyncio task
6. cleanup_tasks: реализованы (stub=False), возвращают ожидаемые поля
7. eager-mode: задачи можно вызвать синхронно (без worker)

Запуск:
    pytest tests/smoke/test_sprint7_phase_c3_celery_tasks.py -v
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# Гарантируем что telegram_bot_token задан (miniapp/backend/config.py валидирует)
import os
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000:test-token-for-tests")


# ============================================================
# 1. Регистрация всех tasks
# ============================================================
@pytest.fixture(scope="module")
def celery_app_with_tasks():
    """Загружает Celery app и принудительно импортирует все task-модули."""
    from worker.celery_app import app
    app.loader.import_default_modules()
    return app


def test_all_phase_c3_tasks_registered(celery_app_with_tasks):
    """Все 9 задач Фазы C.3 зарегистрированы в Celery app."""
    app = celery_app_with_tasks
    expected = [
        "worker.tasks.gibdd_tasks.execute_pipeline_task",
        "worker.tasks.gibdd_tasks.fetch_cards_task",
        "worker.tasks.llm_tasks.llm_summary_task",
        "worker.tasks.llm_tasks.llm_qa_task",
        "worker.tasks.clusters_tasks.clusters_calc_task",
        "worker.tasks.exports_tasks.generate_excel_task",
        "worker.tasks.exports_tasks.generate_map_task",
        "worker.tasks.cleanup_tasks.cleanup_expired_caches",
        "worker.tasks.cleanup_tasks.flush_stale_task_states",
    ]
    for name in expected:
        assert name in app.tasks, f"Task {name} not registered"


def test_tasks_bound_to_correct_queues(celery_app_with_tasks):
    """Каждая задача привязана к правильной очереди."""
    app = celery_app_with_tasks

    queue_mapping = {
        "worker.tasks.gibdd_tasks.execute_pipeline_task": "gibdd",
        "worker.tasks.gibdd_tasks.fetch_cards_task": "gibdd",
        "worker.tasks.llm_tasks.llm_summary_task": "llm",
        "worker.tasks.llm_tasks.llm_qa_task": "llm",
        "worker.tasks.clusters_tasks.clusters_calc_task": "clusters",
        "worker.tasks.exports_tasks.generate_excel_task": "exports",
        "worker.tasks.exports_tasks.generate_map_task": "exports",
    }
    for task_name, expected_queue in queue_mapping.items():
        task = app.tasks[task_name]
        assert task.queue == expected_queue, (
            f"{task_name}: expected queue={expected_queue}, got {task.queue}"
        )


def test_all_tasks_have_acks_late(celery_app_with_tasks):
    """Все задачи используют acks_late для надёжности."""
    app = celery_app_with_tasks
    task_names = [
        "worker.tasks.gibdd_tasks.execute_pipeline_task",
        "worker.tasks.llm_tasks.llm_summary_task",
        "worker.tasks.clusters_tasks.clusters_calc_task",
        "worker.tasks.exports_tasks.generate_excel_task",
    ]
    for name in task_names:
        task = app.tasks[name]
        assert task.acks_late is True, f"{name}: acks_late should be True"


# ============================================================
# 2. task_state (in-memory fallback без Redis)
# ============================================================
def test_task_state_healthcheck_in_memory():
    """task_state.healthcheck возвращает in_memory backend без Redis."""
    from worker import task_state
    # Сброс кэша клиента — гарантирует что мы видим актуальное состояние
    task_state._redis_client = None
    task_state._redis_client_checked = False

    health = task_state.healthcheck()
    assert isinstance(health, dict)
    assert "available" in health
    assert "backend" in health
    assert "active_task_states" in health
    # Без Redis — fallback на in_memory
    assert health["available"] is False
    assert health["backend"] == "in_memory"


def test_task_state_save_load_in_memory_fallback():
    """save_task_state возвращает False без Redis (fallback на _tasks)."""
    from worker import task_state
    task_state._redis_client = None
    task_state._redis_client_checked = False

    class _StubTask:
        id = "test-123"
        user_id = 1
        region_code = "1141"
        region_name = "Ленинская обл."
        period_label = "7 мес. 2026"
        dat_list = ["1.2026"]
        raw_query = ""
        status = "pending"
        progress = 0
        error = None
        files = []
        analytics = None
        total_dtp = 0
        total_dead = 0
        total_injured = 0
        created_at = datetime.now(timezone.utc)
        updated_at = datetime.now(timezone.utc)
        llm_summary_state = None
        clusters_state = None

    # save_task_state без Redis — возвращает False
    result = task_state.save_task_state(_StubTask)
    assert result is False

    # load_task_state без Redis — возвращает None
    snapshot = task_state.load_task_state("test-123")
    assert snapshot is None

    # delete_task_state без Redis — возвращает False
    deleted = task_state.delete_task_state("test-123")
    assert deleted is False


def test_task_state_to_snapshot_serialization():
    """task_to_snapshot корректно сериализует Task в dict."""
    from worker import task_state

    class _StubTask:
        id = "test-456"
        user_id = 42
        region_code = "1141"
        region_name = "Ленинградская область"
        period_label = "7 мес. 2026"
        dat_list = ["1.2026", "2.2026"]
        raw_query = "[structured] ..."
        status = "fetching"
        progress = 20
        error = None
        files = [{"type": "dtp_cards", "filename": "test.xlsx", "size_bytes": 1024}]
        analytics = {"total_dtp": 100}
        total_dtp = 100
        total_dead = 5
        total_injured = 50
        created_at = datetime(2026, 8, 13, 10, 0, 0, tzinfo=timezone.utc)
        updated_at = datetime(2026, 8, 13, 10, 0, 5, tzinfo=timezone.utc)
        llm_summary_state = None
        clusters_state = None

    snapshot = task_state.task_to_snapshot(_StubTask)
    assert snapshot["id"] == "test-456"
    assert snapshot["user_id"] == 42
    assert snapshot["region_code"] == "1141"
    assert snapshot["status"] == "fetching"
    assert snapshot["progress"] == 20
    assert snapshot["total_dtp"] == 100
    assert snapshot["total_dead"] == 5
    assert snapshot["total_injured"] == 50
    assert snapshot["dat_list"] == ["1.2026", "2.2026"]
    assert snapshot["files"][0]["filename"] == "test.xlsx"
    assert snapshot["_source"] == "celery_v1"
    # created_at сериализован в ISO
    assert "2026-08-13" in snapshot["created_at"]


# ============================================================
# 3. redis_pubsub (in-memory path)
# ============================================================
def test_redis_pubsub_healthcheck_in_memory():
    """redis_pubsub.healthcheck возвращает in_memory без Redis."""
    from worker import redis_pubsub
    redis_pubsub._redis_client = None
    redis_pubsub._redis_client_checked = False

    health = redis_pubsub.healthcheck()
    assert isinstance(health, dict)
    assert health["backend"] == "in_memory"
    assert health["available"] is False


def test_redis_pubsub_in_memory_publish_subscribe():
    """In-memory pub/sub: publish → subscribe получает сообщение."""
    from worker import redis_pubsub
    redis_pubsub._redis_client = None
    redis_pubsub._redis_client_checked = False

    received = []

    async def _test():
        # Запускаем subscriber в фоне
        async def _consume():
            async for msg in redis_pubsub.subscribe(
                "test-task", suffix="llm", timeout=2.0
            ):
                received.append(msg)
                if msg.get("type") in ("done", "error"):
                    return

        consumer_task = asyncio.create_task(_consume())
        # Даём подписчику время стартовать
        await asyncio.sleep(0.05)

        # Публикуем токены + done
        redis_pubsub.publish_token("test-task", "Hello", suffix="llm")
        redis_pubsub.publish_token("test-task", " world", suffix="llm")
        redis_pubsub.publish_done("test-task", "Hello world", suffix="llm")

        # Ждём завершения consumer'а
        await asyncio.wait_for(consumer_task, timeout=2.0)

    asyncio.run(_test())

    # Должны получить 3 сообщения: token, token, done
    assert len(received) == 3
    assert received[0]["type"] == "token"
    assert received[0]["data"] == "Hello"
    assert received[1]["type"] == "token"
    assert received[1]["data"] == " world"
    assert received[2]["type"] == "done"
    assert received[2]["data"]["text"] == "Hello world"


# ============================================================
# 4. dispatcher (in-memory mode)
# ============================================================
def test_dispatcher_healthcheck_in_memory():
    """dispatcher.healthcheck возвращает in_memory без Celery."""
    from worker import dispatcher
    # Без REDIS_URL — in_memory
    health = dispatcher.healthcheck()
    assert health["backend"] in ("in_memory", "celery")
    # В тестовом окружении без Redis — in_memory
    if not health["celery_enabled"]:
        assert health["backend"] == "in_memory"


def test_dispatcher_is_celery_enabled_without_redis():
    """_is_celery_enabled возвращает False без REDIS_URL."""
    from worker import dispatcher
    result = dispatcher._is_celery_enabled()
    # Без REDIS_URL в тестовом окружении — False
    assert result is False


def test_dispatch_execute_pipeline_in_memory_returns_task_id():
    """dispatch_execute_pipeline возвращает task_id и не падает в in-memory."""
    from worker import dispatcher

    # В in-memory режиме диспетчер попытается импортировать pipeline.execute_task
    # и запустить его через asyncio.create_task. Если task_id не существует —
    # pipeline.execute_task просто вернётся (no-op).
    # Используем фейковый task_id.
    task_id = dispatcher.dispatch_execute_pipeline(
        task_id="nonexistent-task-id-for-test",
        dat_list=["1.2026"],
        reg_code="1141",
        region_name="Test Region",
        period_label="Test Period",
    )
    assert task_id == "nonexistent-task-id-for-test"


# ============================================================
# 5. cleanup_tasks (реализованы, не stubs)
# ============================================================
def test_cleanup_expired_caches_returns_real_result(celery_app_with_tasks):
    """cleanup_expired_caches возвращает реальный результат (не stub)."""
    from worker.tasks.cleanup_tasks import cleanup_expired_caches

    app = celery_app_with_tasks
    old_value = app.conf.task_always_eager
    app.conf.task_always_eager = True
    try:
        result = cleanup_expired_caches.apply().get(timeout=15)
    finally:
        app.conf.task_always_eager = old_value

    assert isinstance(result, dict)
    assert result.get("stub") is False  # Реализация, не заглушка
    assert "cards_deleted" in result
    assert "clusters_deleted" in result
    assert "excel_deleted" in result
    assert "llm_deleted" in result
    assert "total_deleted" in result
    assert "errors" in result
    # В тестовом окружении без БД — все счётчики 0
    assert result["cards_deleted"] == 0
    assert result["total_deleted"] == 0


def test_flush_stale_task_states_returns_real_result(celery_app_with_tasks):
    """flush_stale_task_states возвращает реальный результат (не stub)."""
    from worker.tasks.cleanup_tasks import flush_stale_task_states

    app = celery_app_with_tasks
    old_value = app.conf.task_always_eager
    app.conf.task_always_eager = True
    try:
        result = flush_stale_task_states.apply(
            kwargs={"max_age_seconds": 86400}
        ).get(timeout=10)
    finally:
        app.conf.task_always_eager = old_value

    assert isinstance(result, dict)
    assert result.get("stub") is False
    assert "task_states_deleted" in result
    assert "total_active" in result
    # Без Redis — 0
    assert result["task_states_deleted"] == 0


# ============================================================
# 6. Структура файлов Фазы C.3
# ============================================================
def test_phase_c3_files_exist():
    """Все файлы Фазы C.3 существуют."""
    worker_dir = _PROJECT_ROOT / "worker"
    assert (worker_dir / "task_state.py").exists()
    assert (worker_dir / "redis_pubsub.py").exists()
    assert (worker_dir / "dispatcher.py").exists()
    assert (worker_dir / "tasks" / "gibdd_tasks.py").exists()
    assert (worker_dir / "tasks" / "llm_tasks.py").exists()
    assert (worker_dir / "tasks" / "clusters_tasks.py").exists()
    assert (worker_dir / "tasks" / "exports_tasks.py").exists()


def test_phase_c3_modules_importable():
    """Все модули Фазы C.3 импортируются без ошибок."""
    from worker import task_state, redis_pubsub, dispatcher
    from worker.tasks import (
        clusters_tasks,
        exports_tasks,
        gibdd_tasks,
        llm_tasks,
    )

    # Проверяем что модули имеют ожидаемые функции
    assert hasattr(gibdd_tasks, "execute_pipeline_task")
    assert hasattr(gibdd_tasks, "fetch_cards_task")
    assert hasattr(llm_tasks, "llm_summary_task")
    assert hasattr(llm_tasks, "llm_qa_task")
    assert hasattr(clusters_tasks, "clusters_calc_task")
    assert hasattr(exports_tasks, "generate_excel_task")
    assert hasattr(exports_tasks, "generate_map_task")

    # Helper функции
    assert hasattr(task_state, "save_task_state")
    assert hasattr(task_state, "load_task_state")
    assert hasattr(task_state, "delete_task_state")
    assert hasattr(task_state, "healthcheck")
    assert hasattr(redis_pubsub, "publish_token")
    assert hasattr(redis_pubsub, "publish_done")
    assert hasattr(redis_pubsub, "publish_error")
    assert hasattr(redis_pubsub, "subscribe")
    assert hasattr(dispatcher, "dispatch_execute_pipeline")
    assert hasattr(dispatcher, "dispatch_llm_summary")
    assert hasattr(dispatcher, "dispatch_llm_qa")
    assert hasattr(dispatcher, "dispatch_clusters_calc")


# ============================================================
# 7. Celery app includes (раскомментированы)
# ============================================================
def test_celery_app_includes_all_phase_c3_modules(celery_app_with_tasks):
    """Celery app include содержит все 5 task-модулей Фазы C.3."""
    app = celery_app_with_tasks
    # Celery 5+ хранит include в app.conf.include или app.include
    includes = app.conf.get("include", []) or []
    expected = [
        "worker.tasks.gibdd_tasks",
        "worker.tasks.llm_tasks",
        "worker.tasks.clusters_tasks",
        "worker.tasks.exports_tasks",
        "worker.tasks.cleanup_tasks",
    ]
    for mod in expected:
        assert mod in includes, f"Module {mod} not in Celery include"


# ============================================================
# 8. Конфигурация задач (time limits, retries)
# ============================================================
def test_execute_pipeline_task_has_zero_retries(celery_app_with_tasks):
    """execute_pipeline_task не ретраится (длинный pipeline лучше пересоздать)."""
    app = celery_app_with_tasks
    task = app.tasks["worker.tasks.gibdd_tasks.execute_pipeline_task"]
    assert task.max_retries == 0


def test_fetch_cards_task_has_retries(celery_app_with_tasks):
    """fetch_cards_task ретраится 2 раза (transient API errors)."""
    app = celery_app_with_tasks
    task = app.tasks["worker.tasks.gibdd_tasks.fetch_cards_task"]
    assert task.max_retries == 2


def test_llm_tasks_have_one_retry(celery_app_with_tasks):
    """llm_summary_task и llm_qa_task имеют 1 retry (LLM 429/503)."""
    app = celery_app_with_tasks
    summary_task = app.tasks["worker.tasks.llm_tasks.llm_summary_task"]
    qa_task = app.tasks["worker.tasks.llm_tasks.llm_qa_task"]
    assert summary_task.max_retries == 1
    assert qa_task.max_retries == 1
