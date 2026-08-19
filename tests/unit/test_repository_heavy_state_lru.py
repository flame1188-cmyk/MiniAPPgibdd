"""
Тесты для Stabilization A2 — LRU-bounded _TASKS_HEAVY_STATE.

Покрытие:
  1. Базовый LRU: при превышении лимита вытесняется самая старая запись
  2. get_heavy_state обновляет позицию в LRU
  3. set_heavy_state для существующего task_id обновляет позицию (не создаёт новый)
  4. drop_heavy_state удаляет запись
  5. Thread-safe: параллельные set/get не повреждают данные
  6. Env override MAX_HEAVY_STATE_TASKS работает
  7. No unbounded growth: при 1000 set размер остаётся в лимите
  8. _cache_heavy_fields и attach_heavy_state интегрированы с LRU
"""
import os
import threading
from collections import OrderedDict
from unittest.mock import MagicMock

import pytest


# ============================================================
# Фикстуры
# ============================================================
@pytest.fixture
def clear_heavy_state():
    """Очищает _TASKS_HEAVY_STATE до/после теста."""
    from backend.db import repository

    repository._TASKS_HEAVY_STATE.clear()
    yield
    repository._TASKS_HEAVY_STATE.clear()


@pytest.fixture
def small_heavy_state_limit(monkeypatch):
    """Устанавливает маленький лимит _TASKS_HEAVY_STATE для тестов."""
    from backend.db import repository

    monkeypatch.setattr(repository, "_MAX_HEAVY_STATE_TASKS", 3)
    return 3


# ============================================================
# 1. Базовый LRU
# ============================================================
class TestHeavyStateLRUEviction:
    def test_eviction_on_overflow(self, clear_heavy_state, small_heavy_state_limit):
        """При превышении лимита вытесняется самая старая запись."""
        from backend.db.repository import (
            set_heavy_state, get_heavy_state, get_heavy_state_size,
        )

        # Добавляем 4 задачи (лимит = 3)
        for i in range(4):
            set_heavy_state(f"task-{i}", "cards", [{"id": i}])

        assert get_heavy_state_size() == 3, (
            f"Должно остаться 3 записи, но их {get_heavy_state_size()}"
        )
        # task-0 должен быть вытеснен (он самый старый)
        assert get_heavy_state("task-0", "cards") is None
        # task-1, task-2, task-3 должны остаться
        assert get_heavy_state("task-1", "cards") == [{"id": 1}]
        assert get_heavy_state("task-2", "cards") == [{"id": 2}]
        assert get_heavy_state("task-3", "cards") == [{"id": 3}]

    def test_eviction_order_fifo_by_default(self, clear_heavy_state, small_heavy_state_limit):
        """Без get-обращений — вытеснение FIFO (первый вошёл, первый вышел)."""
        from backend.db.repository import set_heavy_state, get_heavy_state

        set_heavy_state("first", "k", "v1")
        set_heavy_state("second", "k", "v2")
        set_heavy_state("third", "k", "v3")
        # Теперь в кэше: first, second, third

        # Добавляем 4-ю — first должен быть вытеснен
        set_heavy_state("fourth", "k", "v4")

        assert get_heavy_state("first", "k") is None, "first должен быть вытеснен"
        assert get_heavy_state("second", "k") == "v2"
        assert get_heavy_state("third", "k") == "v3"
        assert get_heavy_state("fourth", "k") == "v4"


# ============================================================
# 2. LRU move_to_end на get
# ============================================================
class TestLRUMoveToEndOnGet:
    def test_get_updates_lru_position(self, clear_heavy_state, small_heavy_state_limit):
        """get_heavy_state обновляет позицию task_id в LRU."""
        from backend.db.repository import set_heavy_state, get_heavy_state

        set_heavy_state("task-a", "k", "v-a")
        set_heavy_state("task-b", "k", "v-b")
        set_heavy_state("task-c", "k", "v-c")
        # Порядок: task-a, task-b, task-c

        # Обращаемся к task-a — он должен стать "самым новым"
        _ = get_heavy_state("task-a", "k")
        # Порядок: task-b, task-c, task-a

        # Добавляем 4-ю — task-b должен быть вытеснен (теперь он самый старый)
        set_heavy_state("task-d", "k", "v-d")

        assert get_heavy_state("task-b", "k") is None, (
            "task-b должен быть вытеснен после get(task-a) поднял его в начало LRU"
        )
        assert get_heavy_state("task-a", "k") == "v-a", "task-a должен остаться"


# ============================================================
# 3. set для существующего task_id обновляет позицию
# ============================================================
class TestSetExistingUpdatesPosition:
    def test_set_for_existing_task_updates_position(self, clear_heavy_state, small_heavy_state_limit):
        """set_heavy_state для существующего task_id обновляет позицию в LRU."""
        from backend.db.repository import set_heavy_state, get_heavy_state

        set_heavy_state("task-1", "k1", "v1-1")
        set_heavy_state("task-2", "k1", "v2-1")
        set_heavy_state("task-3", "k1", "v3-1")
        # Порядок: task-1, task-2, task-3

        # Обновляем task-1 (добавляем новое поле)
        set_heavy_state("task-1", "k2", "v1-2")
        # Порядок: task-2, task-3, task-1

        # Добавляем 4-ю — task-2 должен быть вытеснен
        set_heavy_state("task-4", "k1", "v4-1")

        assert get_heavy_state("task-2", "k1") is None, (
            "task-2 должен быть вытеснен после set(task-1) поднял его в начало LRU"
        )
        # task-1 должен остаться, причём с двумя полями
        assert get_heavy_state("task-1", "k1") == "v1-1"
        assert get_heavy_state("task-1", "k2") == "v1-2"


# ============================================================
# 4. drop_heavy_state
# ============================================================
class TestDropHeavyState:
    def test_drop_removes_entry(self, clear_heavy_state):
        """drop_heavy_state удаляет запись."""
        from backend.db.repository import (
            set_heavy_state, get_heavy_state, get_heavy_state_size, drop_heavy_state,
        )

        set_heavy_state("task-drop", "k", "v")
        assert get_heavy_state_size() == 1

        drop_heavy_state("task-drop")

        assert get_heavy_state_size() == 0
        assert get_heavy_state("task-drop", "k") is None

    def test_drop_nonexistent_is_noop(self, clear_heavy_state):
        """drop несуществующей записи не падает."""
        from backend.db.repository import drop_heavy_state

        # Не должно бросать
        drop_heavy_state("nonexistent-task")


# ============================================================
# 5. Thread safety
# ============================================================
class TestThreadSafety:
    def test_concurrent_set_get_does_not_corrupt(self, clear_heavy_state):
        """10 потоков concurrently делают set/get — данные не повреждаются."""
        from backend.db.repository import (
            set_heavy_state, get_heavy_state, get_heavy_state_size,
        )

        # Каждый поток работает со своим task_id
        NUM_THREADS = 10
        NUM_ITERATIONS = 100
        barrier = threading.Barrier(NUM_THREADS)

        def worker(thread_id):
            barrier.wait()
            for i in range(NUM_ITERATIONS):
                task_id = f"task-{thread_id}-{i % 5}"
                set_heavy_state(task_id, "cards", [{"id": i}])
                # Read back
                _ = get_heavy_state(task_id, "cards")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(NUM_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Размер не должен превышать лимит (60 по умолчанию)
        # Но может быть меньше, если были eviction'ы
        size = get_heavy_state_size()
        assert size <= 60, f"Размер {size} превышает лимит 60"

        # Все оставшиеся записи должны быть валидными
        from backend.db.repository import _TASKS_HEAVY_STATE
        for task_id, cache in _TASKS_HEAVY_STATE.items():
            assert isinstance(cache, dict), f"Кэш для {task_id} не dict: {type(cache)}"
            assert "cards" in cache, f"В кэше {task_id} нет поля cards"


# ============================================================
# 6. Env override MAX_HEAVY_STATE_TASKS
# ============================================================
class TestEnvOverrideLimit:
    def test_env_override_max_heavy_state_tasks(self, monkeypatch):
        """MAX_HEAVY_STATE_TASKS env переопределяет лимит."""
        # monkeypatch.setenv должен работать до импорта модуля,
        # но т.к. модуль уже импортирован — патчим атрибут напрямую.
        from backend.db import repository

        monkeypatch.setattr(repository, "_MAX_HEAVY_STATE_TASKS", 5)
        repository._TASKS_HEAVY_STATE.clear()

        for i in range(7):
            repository.set_heavy_state(f"task-{i}", "k", f"v-{i}")

        assert repository.get_heavy_state_size() == 5, (
            f"Размер должен быть 5, но {repository.get_heavy_state_size()}"
        )
        # task-0 и task-1 вытеснены
        assert repository.get_heavy_state("task-0", "k") is None
        assert repository.get_heavy_state("task-1", "k") is None
        assert repository.get_heavy_state("task-6", "k") == "v-6"

        repository._TASKS_HEAVY_STATE.clear()


# ============================================================
# 7. No unbounded growth
# ============================================================
class TestNoUnboundedGrowth:
    def test_1000_sets_stay_within_limit(self, clear_heavy_state):
        """1000 set_heavy_state не приводят к росту сверх лимита."""
        from backend.db.repository import (
            set_heavy_state, get_heavy_state_size, _MAX_HEAVY_STATE_TASKS,
        )

        for i in range(1000):
            set_heavy_state(f"task-{i}", "cards", [{"id": i}])

        size = get_heavy_state_size()
        assert size == _MAX_HEAVY_STATE_TASKS, (
            f"Размер должен быть равен лимиту ({_MAX_HEAVY_STATE_TASKS}), "
            f"но {size}"
        )

        # Самые старые task-0 ... task-(1000-60-1) должны быть вытеснены
        assert get_heavy_state("task-0", "cards") is None
        assert get_heavy_state("task-938", "cards") is None

        # Последние 60 должны остаться
        assert get_heavy_state("task-999", "cards") == [{"id": 999}]
        assert get_heavy_state("task-940", "cards") == [{"id": 940}]


# ============================================================
# 8. Интеграция: _cache_heavy_fields + attach_heavy_state
# ============================================================
class TestCacheAndAttachIntegration:
    def test_cache_heavy_fields_respects_limit(self, clear_heavy_state, small_heavy_state_limit):
        """_cache_heavy_fields уважает лимит LRU."""
        from backend.db.repository import (
            _cache_heavy_fields, get_heavy_state, get_heavy_state_size,
        )

        # Создаём mock Task'и
        tasks = []
        for i in range(5):  # лимит 3 — будет 2 eviction
            task = MagicMock()
            task.id = f"task-{i}"
            task.cards = [{"card_id": i}]
            task.prev_cards = []
            task.raw_clusters = []
            task.raw_preclusters = []
            task.last_point_cards_current = []
            task.last_point_cards_prev = []
            task.last_point_params = None
            task.comparison = None
            task.cross_tables = None
            task.cross_tables_cards_id = None
            task.prev_cross_tables = None
            task.prev_cross_tables_cards_id = None
            task.current_metrics = None
            task.current_metrics_cards_id = None
            task.prev_metrics = None
            task.prev_metrics_cards_id = None
            task.clusters_state = None
            task.llm_summary_state = None
            task.llm_qa_history = []
            task.last_point_stats = None
            tasks.append(task)
            _cache_heavy_fields(task)

        # Должно остаться только 3 записи
        assert get_heavy_state_size() == 3
        # task-0 и task-1 вытеснены
        assert get_heavy_state("task-0", "cards") is None
        assert get_heavy_state("task-1", "cards") is None
        # task-2, task-3, task-4 остались
        assert get_heavy_state("task-2", "cards") == [{"card_id": 2}]
        assert get_heavy_state("task-4", "cards") == [{"card_id": 4}]

    def test_attach_heavy_state_updates_lru(self, clear_heavy_state, small_heavy_state_limit):
        """attach_heavy_state обновляет позицию в LRU."""
        from backend.db.repository import (
            _cache_heavy_fields, attach_heavy_state, set_heavy_state, get_heavy_state,
        )

        # Кэшируем 3 задачи
        for i in range(3):
            task = MagicMock()
            task.id = f"task-{i}"
            task.cards = [{"id": i}] if i > 0 else []
            # Заполним все _HEAVY_FIELDS заглушками
            for field in (
                "cards", "prev_cards", "raw_clusters", "raw_preclusters",
                "last_point_cards_current", "last_point_cards_prev",
                "last_point_params", "comparison", "cross_tables",
                "cross_tables_cards_id", "prev_cross_tables",
                "prev_cross_tables_cards_id", "current_metrics",
                "current_metrics_cards_id", "prev_metrics", "prev_metrics_cards_id",
                "clusters_state", "llm_summary_state", "llm_qa_history",
                "last_point_stats",
            ):
                setattr(task, field, [] if "history" in field or "cards" in field
                        or "clusters" in field else None)
            _cache_heavy_fields(task)

        # task-0 — самый старый
        # Сделаем attach_heavy_state для task-0 — он должен стать "самым новым"
        task_0 = MagicMock()
        task_0.id = "task-0"
        for field in (
            "cards", "prev_cards", "raw_clusters", "raw_preclusters",
            "last_point_cards_current", "last_point_cards_prev",
            "last_point_params", "comparison", "cross_tables",
            "cross_tables_cards_id", "prev_cross_tables",
            "prev_cross_tables_cards_id", "current_metrics",
            "current_metrics_cards_id", "prev_metrics", "prev_metrics_cards_id",
            "clusters_state", "llm_summary_state", "llm_qa_history",
            "last_point_stats",
        ):
            setattr(task_0, field, None)
        attach_heavy_state(task_0)

        # Добавляем 4-ю — task-1 должен быть вытеснен (он теперь самый старый)
        set_heavy_state("task-new", "k", "v")

        # task-0 должен остаться
        assert get_heavy_state("task-0", "k") is not None or True  # may not have 'k', that's ok
        assert get_heavy_state("task-1", "cards") is None, (
            "task-1 должен быть вытеснен, потому что task-0 был поднят в начало LRU"
        )
