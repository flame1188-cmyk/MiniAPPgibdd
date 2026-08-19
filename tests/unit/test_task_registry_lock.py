"""
Тесты для Stabilization A1 — per-task asyncio.Lock в task_registry.

Покрытие:
  1. _get_task_lock возвращает тот же Lock для одного task_id
  2. _get_task_lock создаёт разные Lock для разных task_id
  3. Конкурентные merge одной задачи сериализуются (через asyncio.Lock)
  4. Lock освобождается после исключения в merge
  5. LRU eviction удаляет per-task lock
  6. unregister_task удаляет per-task lock
  7. Lock cleanup не течёт (после eviction + unregister _task_locks пуст)
  8. get_task_async возвращает консистентное состояние (никакого полуприменённого)
  9. _maybe_merge_redis_snapshot_sync остаётся работоспособным (backward compat)
"""
import asyncio
import threading
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from backend.services.models import Task, TaskStatus


# ============================================================
# Фикстуры
# ============================================================
@pytest.fixture
def clear_task_state():
    """Очищает _tasks и _task_locks до/после теста."""
    from backend.services import task_registry

    task_registry._tasks.clear()
    task_registry._clear_task_locks_for_tests()
    yield
    task_registry._tasks.clear()
    task_registry._clear_task_locks_for_tests()


@pytest.fixture
def make_task():
    """Фабрика для создания тестового Task."""
    def _make(task_id="test-task-1", user_id=123):
        return Task(
            id=task_id,
            user_id=user_id,
            region_code="77",
            region_name="Москва",
            period_label="2025",
            dat_list=["2025"],
            raw_query="Москва 2025",
        )
    return _make


# ============================================================
# _get_task_lock — базовая функциональность
# ============================================================
class TestGetTaskLock:
    def test_same_task_id_returns_same_lock(self, clear_task_state):
        """Два вызова с одним task_id возвращают тот же Lock объект."""
        from backend.services.task_registry import _get_task_lock

        lock1 = _get_task_lock("task-1")
        lock2 = _get_task_lock("task-1")
        assert lock1 is lock2, "Lock для одного task_id должен быть одним объектом"

    def test_different_task_ids_return_different_locks(self, clear_task_state):
        """Разные task_id → разные Lock объекты."""
        from backend.services.task_registry import _get_task_lock

        lock1 = _get_task_lock("task-1")
        lock2 = _get_task_lock("task-2")
        assert lock1 is not lock2, "Lock для разных task_id должен быть разным"

    def test_lock_creation_is_thread_safe(self, clear_task_state):
        """Параллельные потоки получают тот же Lock для одного task_id."""
        from backend.services.task_registry import _get_task_lock

        locks = []
        barrier = threading.Barrier(10)

        def worker():
            barrier.wait()
            locks.append(_get_task_lock("task-concurrent"))

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Все 10 потоков должны получить один и тот же Lock
        assert len(locks) == 10
        first = locks[0]
        assert all(l is first for l in locks), (
            "Все потоки должны получить тот же Lock объект — "
            f"получили: {[(l is first) for l in locks]}"
        )


# ============================================================
# _drop_task_lock — cleanup
# ============================================================
class TestDropTaskLock:
    def test_drop_removes_lock(self, clear_task_state):
        """_drop_task_lock удаляет Lock из dict."""
        from backend.services.task_registry import (
            _get_task_lock, _drop_task_lock, _task_locks,
        )

        _get_task_lock("task-drop-me")
        assert "task-drop-me" in _task_locks

        _drop_task_lock("task-drop-me")
        assert "task-drop-me" not in _task_locks

    def test_drop_nonexistent_is_noop(self, clear_task_state):
        """Удаление несуществующего Lock не падает."""
        from backend.services.task_registry import _drop_task_lock

        # Не должно бросать
        _drop_task_lock("nonexistent-task")

    def test_drop_then_get_creates_new_lock(self, clear_task_state):
        """После drop, get создаёт новый Lock объект."""
        from backend.services.task_registry import (
            _get_task_lock, _drop_task_lock,
        )

        lock1 = _get_task_lock("task-recreate")
        _drop_task_lock("task-recreate")
        lock2 = _get_task_lock("task-recreate")
        assert lock1 is not lock2, "После drop+get должен быть новый Lock"


# ============================================================
# _maybe_merge_redis_snapshot — сериализация
# ============================================================
class TestMergeSnapshotSerialization:
    @pytest.mark.asyncio
    async def test_concurrent_merges_are_serialized(self, clear_task_state, make_task):
        """Два конкурентных merge одной задачи не могут пересечься."""
        from backend.services import task_registry

        task = make_task(task_id="concurrent-task")
        task_registry._tasks[task.id] = task

        # Счётчик активных одновременных merge
        active_count = 0
        max_active = 0
        counter_lock = threading.Lock()

        # Мокаем _maybe_merge_redis_snapshot_sync, чтобы он задерживался
        # и мы могли проверить, что два вызова не пересекаются.
        original_sync = task_registry._maybe_merge_redis_snapshot_sync

        def slow_sync(t):
            nonlocal active_count, max_active
            with counter_lock:
                active_count += 1
                max_active = max(max_active, active_count)
            # Имитируем долгую работу (10ms)
            import time
            time.sleep(0.01)
            with counter_lock:
                active_count -= 1
            # Реально ничего не делаем — это тест
            return None

        task_registry._maybe_merge_redis_snapshot_sync = slow_sync

        try:
            # Запускаем 5 конкурентных merge
            await asyncio.gather(
                task_registry._maybe_merge_redis_snapshot(task),
                task_registry._maybe_merge_redis_snapshot(task),
                task_registry._maybe_merge_redis_snapshot(task),
                task_registry._maybe_merge_redis_snapshot(task),
                task_registry._maybe_merge_redis_snapshot(task),
            )

            # Если бы Lock не было — max_active могло быть > 1
            assert max_active == 1, (
                f"Конкурентные merge должны быть сериализованы (max_active={max_active}, "
                "ожидалось 1). Возможно, Lock не работает."
            )
        finally:
            task_registry._maybe_merge_redis_snapshot_sync = original_sync

    @pytest.mark.asyncio
    async def test_lock_released_after_exception(self, clear_task_state, make_task):
        """Если merge выбросил исключение, lock освобождается."""
        from backend.services import task_registry

        task = make_task(task_id="exc-task")
        task_registry._tasks[task.id] = task

        # Мокаем sync-функцию, чтобы она кидала исключение
        original_sync = task_registry._maybe_merge_redis_snapshot_sync

        def throwing_sync(t):
            raise RuntimeError("Test exception")

        task_registry._maybe_merge_redis_snapshot_sync = throwing_sync

        try:
            # Первый вызов — должен выбросить RuntimeError
            with pytest.raises(RuntimeError, match="Test exception"):
                await task_registry._maybe_merge_redis_snapshot(task)

            # Проверяем, что Lock освобождён — можем взять его снова
            lock = task_registry._get_task_lock(task.id)
            assert not lock.locked(), "Lock должен быть освобождён после исключения"

            # Второй вызов тоже должен пройти (не зависнуть)
            with pytest.raises(RuntimeError):
                await task_registry._maybe_merge_redis_snapshot(task)
        finally:
            task_registry._maybe_merge_redis_snapshot_sync = original_sync

    @pytest.mark.asyncio
    async def test_different_tasks_can_merge_in_parallel(self, clear_task_state, make_task):
        """Merge разных задач идёт параллельно (не блокирует друг друга)."""
        from backend.services import task_registry

        task1 = make_task(task_id="task-parallel-1")
        task2 = make_task(task_id="task-parallel-2")
        task_registry._tasks[task1.id] = task1
        task_registry._tasks[task2.id] = task2

        # Мокаем sync-функцию с задержкой
        original_sync = task_registry._maybe_merge_redis_snapshot_sync

        started_events = []
        finished_order = []

        def slow_sync(t):
            started_events.append(t.id)
            import time
            time.sleep(0.05)
            finished_order.append(t.id)
            return None

        task_registry._maybe_merge_redis_snapshot_sync = slow_sync

        try:
            # Запускаем оба merge одновременно
            start = asyncio.get_event_loop().time()
            await asyncio.gather(
                task_registry._maybe_merge_redis_snapshot(task1),
                task_registry._maybe_merge_redis_snapshot(task2),
            )
            elapsed = asyncio.get_event_loop().time() - start

            # Если бы был общий lock — заняло бы ~100ms (последовательно)
            # С per-task lock — ~50ms (параллельно)
            assert elapsed < 0.1, (
                f"Merge разных задач должен идти параллельно (elapsed={elapsed:.3f}s, "
                "ожидалось < 0.1s). Возможно, используется общий Lock вместо per-task."
            )
        finally:
            task_registry._maybe_merge_redis_snapshot_sync = original_sync


# ============================================================
# unregister_task — cleanup per-task lock
# ============================================================
class TestUnregisterTaskLockCleanup:
    def test_unregister_removes_lock(self, clear_task_state, make_task):
        """unregister_task удаляет per-task Lock."""
        from backend.services import task_registry

        task = make_task(task_id="task-unregister")
        task_registry._tasks[task.id] = task
        # Создаём Lock (имитируя активное использование)
        lock_before = task_registry._get_task_lock(task.id)
        assert task.id in task_registry._task_locks

        result = task_registry.unregister_task(task.id, user_id=task.user_id)

        assert result is True
        assert task.id not in task_registry._task_locks, (
            "Lock должен быть удалён после unregister_task"
        )

    def test_unregister_nonexistent_task_no_lock_leak(self, clear_task_state):
        """unregister несуществующей задачи не создаёт Lock."""
        from backend.services import task_registry

        result = task_registry.unregister_task("nonexistent-task")
        assert result is False
        # Не должно создаться Lock при попытке unregister
        assert "nonexistent-task" not in task_registry._task_locks

    def test_unregister_with_user_mismatch_keeps_lock(self, clear_task_state, make_task):
        """unregister с user_id mismatch не удаляет Lock (защита от race)."""
        from backend.services import task_registry

        task = make_task(task_id="task-mismatch", user_id=111)
        task_registry._tasks[task.id] = task
        task_registry._get_task_lock(task.id)

        # Пытаемся удалить с другим user_id
        result = task_registry.unregister_task(task.id, user_id=999)

        assert result is False
        # Lock должен остаться (задача всё ещё в _tasks)
        assert task.id in task_registry._task_locks


# ============================================================
# LRU eviction — lock cleanup
# ============================================================
class TestLRUEvictionLockCleanup:
    def test_eviction_removes_lock(self, clear_task_state, make_task):
        """Когда задача вытесняется из LRU, её Lock удаляется."""
        from backend.services import task_registry
        from backend.services import gibdd_service

        # Устанавливаем маленький лимит для теста
        original_limit = gibdd_service.MAX_INMEMORY_TASKS
        gibdd_service.MAX_INMEMORY_TASKS = 2

        try:
            # Создаём 3 задачи — 3-я вытеснит 1-ю
            t1 = make_task(task_id="evict-1")
            t2 = make_task(task_id="evict-2")
            t3 = make_task(task_id="evict-3")

            task_registry._register_task(t1)
            task_registry._get_task_lock(t1.id)  # создаём Lock для t1
            task_registry._register_task(t2)
            task_registry._register_task(t3)

            # t1 должен быть вытеснен
            assert t1.id not in task_registry._tasks
            # И Lock должен быть удалён
            assert t1.id not in task_registry._task_locks, (
                "Lock вытесненной задачи должен быть удалён из _task_locks"
            )
            # Новые задачи должны остаться
            assert t2.id in task_registry._tasks
            assert t3.id in task_registry._tasks
        finally:
            gibdd_service.MAX_INMEMORY_TASKS = original_limit


# ============================================================
# Backward compatibility: sync функция остаётся работоспособной
# ============================================================
class TestBackwardCompatSync:
    def test_sync_function_still_callable(self, clear_task_state, make_task):
        """_maybe_merge_redis_snapshot_sync остаётся callable для sync callers."""
        from backend.services import task_registry

        task = make_task(task_id="sync-test")
        task_registry._tasks[task.id] = task

        # Sync вызов должен работать без ошибок
        # (внутри load_task_state будет ImportError — это норма в тестах)
        task_registry._maybe_merge_redis_snapshot_sync(task)
        # Если не упало — тест прошёл

    @pytest.mark.asyncio
    async def test_async_function_calls_sync_under_lock(self, clear_task_state, make_task):
        """Async версия вызывает sync под per-task Lock."""
        from backend.services import task_registry

        task = make_task(task_id="async-calls-sync")
        task_registry._tasks[task.id] = task

        # Мокаем sync, чтобы засечь вызов
        called = []
        original = task_registry._maybe_merge_redis_snapshot_sync

        def spy(t):
            called.append(t.id)

        task_registry._maybe_merge_redis_snapshot_sync = spy
        try:
            await task_registry._maybe_merge_redis_snapshot(task)
            assert called == [task.id], "Sync функция должна быть вызвана с правильным task"
        finally:
            task_registry._maybe_merge_redis_snapshot_sync = original


# ============================================================
# Интеграция: get_task_async возвращает консистентное состояние
# ============================================================
class TestGetTaskAsyncConsistency:
    @pytest.mark.asyncio
    async def test_get_task_async_returns_consistent_state(self, clear_task_state, make_task):
        """После get_task_async task либо полностью merged, либо не merged."""
        from backend.services import task_registry

        task = make_task(task_id="consistency-test")
        task.status = TaskStatus.FETCHING
        task.progress = 30
        task_registry._tasks[task.id] = task

        # Мокаем merge, чтобы он "вешал" task, имитируя race condition
        # Если Lock работает — race condition не произойдёт
        original = task_registry._maybe_merge_redis_snapshot_sync

        def mock_merge(t):
            # Имитируем, что merge меняет поля
            t.status = TaskStatus.DONE
            t.progress = 100
            t.files = [{"name": "test.xlsx", "url": "/files/test.xlsx"}]
            t.total_dtp = 100
            t.total_dead = 5
            t.total_injured = 120

        task_registry._maybe_merge_redis_snapshot_sync = mock_merge
        try:
            result = await task_registry.get_task_async(task.id)
            assert result is not None
            # Должны увидеть полностью применённое состояние
            assert result.status == TaskStatus.DONE
            assert result.progress == 100
            assert len(result.files) == 1
            assert result.total_dtp == 100
        finally:
            task_registry._maybe_merge_redis_snapshot_sync = original

    @pytest.mark.asyncio
    async def test_concurrent_get_task_async_returns_same_state(
        self, clear_task_state, make_task
    ):
        """10 конкурентных get_task_async возвращают консистентное состояние."""
        from backend.services import task_registry

        task = make_task(task_id="concurrent-get")
        task.status = TaskStatus.FETCHING
        task_registry._tasks[task.id] = task

        # Мокаем merge, который медленно меняет поля
        original = task_registry._maybe_merge_redis_snapshot_sync

        def slow_complete_merge(t):
            # Имитируем race: сначала меняем status, потом files
            import time
            t.status = TaskStatus.DONE
            time.sleep(0.01)
            t.progress = 100
            time.sleep(0.01)
            t.files = [{"name": "test.xlsx"}]
            time.sleep(0.01)
            t.total_dtp = 100

        task_registry._maybe_merge_redis_snapshot_sync = slow_complete_merge

        try:
            results = await asyncio.gather(
                *[task_registry.get_task_async(task.id) for _ in range(10)]
            )
            # Все 10 результатов должны быть идентичны
            for r in results:
                assert r is not None
                assert r.status == TaskStatus.DONE
                assert r.progress == 100
                assert len(r.files) == 1
                assert r.total_dtp == 100
        finally:
            task_registry._maybe_merge_redis_snapshot_sync = original


# ============================================================
# Lock cleanup — нет утечек
# ============================================================
class TestNoLockLeaks:
    def test_locks_cleanup_after_full_lifecycle(self, clear_task_state, make_task):
        """Полный lifecycle: register → get → unregister → locks пуст."""
        from backend.services import task_registry

        task = make_task(task_id="lifecycle-test")
        task_registry._register_task(task)

        # Создаём Lock (имитируя активное использование)
        task_registry._get_task_lock(task.id)
        assert len(task_registry._task_locks) == 1

        # Удаляем задачу
        task_registry.unregister_task(task.id, user_id=task.user_id)

        # Locks должны быть пусты
        assert len(task_registry._task_locks) == 0, (
            f"_task_locks должен быть пуст, но содержит: {list(task_registry._task_locks.keys())}"
        )

    def test_locks_snapshot_helper(self, clear_task_state):
        """_get_task_locks_snapshot возвращает копию."""
        from backend.services import task_registry

        task_registry._get_task_lock("snap-1")
        task_registry._get_task_lock("snap-2")
        snap = task_registry._get_task_locks_snapshot()
        assert set(snap.keys()) == {"snap-1", "snap-2"}

        # Мутация snapshot не влияет на оригинал
        snap["snap-3"] = asyncio.Lock()
        assert "snap-3" not in task_registry._task_locks
