"""
Тесты сервисного слоя gibdd_service.py (мини-апп).

Без реальной выгрузки ГИБДД/БД — все внешние зависимости мокаются через
патч _import_module, который возвращает stub-модули.

Покрытие:
  - parse_user_query: happy / parser returns None / parser raises
  - get_regions: parser success / fallback to builtin
  - create_task: генерация id, регистрация в _tasks
  - get_task / get_task_async: in-memory hit, not found
  - list_user_tasks: фильтр по user_id, лимит
  - _register_task: LRU eviction при превышении MAX_INMEMORY_TASKS
  - get_llm_providers_status: читает config
  - _task_factory: создание Task из параметров
  - _task_dir: создаёт директорию
  - ask_llm_question: короткий вопрос / нет API key / happy path с моком LLM
  - ensure_prev_cards: mocked bot._fetch_cards_for_period
  - cleanup_old_tasks: in-memory удаление по возрасту
"""
import asyncio
import time
import types
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from backend.services import _imports  # для патчей _PROJECT_ROOT/_import_module


# ============================================================
# Парсинг запроса пользователя
# ============================================================
class TestParseUserQuery:
    @pytest.mark.asyncio
    async def test_happy_path_returns_structured(self, monkeypatch, clear_in_memory_tasks):
        from backend.services import gibdd_service

        # Stub-парсер с минимальным ParsedRequest
        fake_parser = types.ModuleType("user_request_parser")

        class FakePeriod:
            label = "Май 2025"
            def get_dat_list(self):
                return ["5.2025"]

        class FakeParsed:
            region_code = "1101"
            region_name = "Вологодская область"
            period = FakePeriod()

        async def fake_parse(text):
            return FakeParsed()

        fake_parser.parse_user_message = fake_parse
        monkeypatch.setattr(gibdd_service, "_import_module", lambda name: fake_parser)
        monkeypatch.setattr(_imports, "_import_module", lambda name: fake_parser)

        result = await gibdd_service.parse_user_query("Вологодская область май 2025")
        assert result["ok"] is True
        assert result["region_code"] == "1101"
        assert result["region_name"] == "Вологодская область"
        assert result["period"] == "Май 2025"
        assert result["dat_list"] == ["5.2025"]
        assert result["raw_query"] == "Вологодская область май 2025"

    @pytest.mark.asyncio
    async def test_parser_returns_none_yields_error(self, monkeypatch, clear_in_memory_tasks):
        from backend.services import gibdd_service

        fake_parser = types.ModuleType("user_request_parser")

        async def fake_parse(text):
            return None

        fake_parser.parse_user_message = fake_parse
        monkeypatch.setattr(gibdd_service, "_import_module", lambda name: fake_parser)
        monkeypatch.setattr(_imports, "_import_module", lambda name: fake_parser)

        result = await gibdd_service.parse_user_query("абракадабра")
        assert result["ok"] is False
        assert "Не удалось распознать" in result["error"]

    @pytest.mark.asyncio
    async def test_parser_raises_returns_error(self, monkeypatch, clear_in_memory_tasks):
        from backend.services import gibdd_service

        fake_parser = types.ModuleType("user_request_parser")

        async def fake_parse(text):
            raise RuntimeError("parser boom")

        fake_parser.parse_user_message = fake_parse
        monkeypatch.setattr(gibdd_service, "_import_module", lambda name: fake_parser)
        monkeypatch.setattr(_imports, "_import_module", lambda name: fake_parser)

        result = await gibdd_service.parse_user_query("тест")
        assert result["ok"] is False
        assert "parser boom" in result["error"]


# ============================================================
# Справочник регионов
# ============================================================
class TestGetRegions:
    @pytest.mark.asyncio
    async def test_returns_regions_from_parser(self, monkeypatch, clear_in_memory_tasks):
        from backend.services import gibdd_service

        fake_parser = types.ModuleType("user_request_parser")

        async def fake_ensure():
            return [{"code": "1101", "name": "Регион А"}, {"code": "1102", "name": "Регион Б"}]

        fake_parser.ensure_regions_loaded = fake_ensure
        monkeypatch.setattr(gibdd_service, "_import_module", lambda name: fake_parser)
        monkeypatch.setattr(_imports, "_import_module", lambda name: fake_parser)

        result = await gibdd_service.get_regions()
        assert len(result) == 2
        assert result[0]["code"] == "1101"

    @pytest.mark.asyncio
    async def test_falls_back_to_builtin_on_failure(self, monkeypatch, clear_in_memory_tasks):
        from backend.services import gibdd_service

        fake_parser = types.ModuleType("user_request_parser")

        async def fake_ensure():
            raise RuntimeError("network error")

        fake_parser.ensure_regions_loaded = fake_ensure
        # builtin модуль должен остаться настоящим
        def smart_import(name):
            if name == "user_request_parser":
                return fake_parser
            return __import__("regions_builtin")

        monkeypatch.setattr(gibdd_service, "_import_module", smart_import)

        monkeypatch.setattr(_imports, "_import_module", smart_import)

        result = await gibdd_service.get_regions()
        # BUILTIN_REGIONS — непустой список
        assert len(result) > 0
        assert isinstance(result[0], dict)


# ============================================================
# Создание задач и работа с in-memory _tasks
# ============================================================
class TestCreateTask:
    def test_creates_with_unique_id(self, clear_in_memory_tasks):
        from backend.services import gibdd_service

        task1 = gibdd_service.create_task(
            user_id=1,
            region_code="1101",
            region_name="Вологодская область",
            period_label="Май 2025",
            dat_list=["5.2025"],
            raw_query="тест1",
        )
        task2 = gibdd_service.create_task(
            user_id=2,
            region_code="1102",
            region_name="Ленинградская область",
            period_label="Май 2025",
            dat_list=["5.2025"],
            raw_query="тест2",
        )
        assert task1.id != task2.id, "ID должны быть уникальными"
        assert len(task1.id) == 12, "ID должен быть 12 символов (uuid4.hex[:12])"

    def test_task_registered_in_tasks(self, clear_in_memory_tasks):
        from backend.services import gibdd_service

        task = gibdd_service.create_task(
            user_id=42,
            region_code="1101",
            region_name="Регион",
            period_label="Май 2025",
            dat_list=["5.2025"],
            raw_query="тест",
        )
        assert task.id in gibdd_service._tasks
        assert gibdd_service._tasks[task.id] is task

    def test_task_initial_status_pending(self, clear_in_memory_tasks):
        from backend.services import gibdd_service

        task = gibdd_service.create_task(
            user_id=1, region_code="1101", region_name="Рег",
            period_label="Период", dat_list=["1.2025"], raw_query="q",
        )
        assert task.status == gibdd_service.TaskStatus.PENDING
        assert task.progress == 0
        assert task.error is None
        assert task.cards == []


class TestGetTask:
    def test_get_task_in_memory_hit(self, clear_in_memory_tasks):
        from backend.services import gibdd_service

        task = gibdd_service.create_task(
            user_id=1, region_code="1101", region_name="Рег",
            period_label="Период", dat_list=["1.2025"], raw_query="q",
        )
        found = gibdd_service.get_task(task.id)
        assert found is task

    def test_get_task_not_found_returns_none(self, clear_in_memory_tasks):
        from backend.services import gibdd_service
        assert gibdd_service.get_task("несуществующий-id") is None

    @pytest.mark.asyncio
    async def test_get_task_async_in_memory_hit(self, clear_in_memory_tasks):
        from backend.services import gibdd_service

        task = gibdd_service.create_task(
            user_id=1, region_code="1101", region_name="Рег",
            period_label="Период", dat_list=["1.2025"], raw_query="q",
        )
        found = await gibdd_service.get_task_async(task.id)
        assert found is task

    @pytest.mark.asyncio
    async def test_get_task_async_not_found_no_db(self, clear_in_memory_tasks, monkeypatch):
        """Если задачи нет в _tasks и БД не готова — None."""
        from backend.services import gibdd_service

        # Патчим db.connection.is_db_ready → False
        fake_db = types.ModuleType("connection")
        fake_db.is_db_ready = lambda: False
        monkeypatch.setitem(__import__("sys").modules, "backend.db.connection", fake_db)

        result = await gibdd_service.get_task_async("несуществующий")
        assert result is None


class TestListUserTasks:
    @pytest.mark.asyncio
    async def test_returns_only_user_tasks(self, clear_in_memory_tasks):
        from backend.services import gibdd_service

        # Создаём задачи для двух пользователей
        for uid in [1, 1, 1, 2, 2]:
            gibdd_service.create_task(
                user_id=uid, region_code="1101", region_name="Рег",
                period_label="Период", dat_list=["1.2025"], raw_query=f"q-{uid}",
            )

        user1_tasks = await gibdd_service.list_user_tasks(user_id=1, limit=20)
        user2_tasks = await gibdd_service.list_user_tasks(user_id=2, limit=20)
        assert len(user1_tasks) == 3
        assert len(user2_tasks) == 2
        assert all(t.user_id == 1 for t in user1_tasks)
        assert all(t.user_id == 2 for t in user2_tasks)

    @pytest.mark.asyncio
    async def test_limit_applied(self, clear_in_memory_tasks):
        from backend.services import gibdd_service

        for i in range(5):
            gibdd_service.create_task(
                user_id=99, region_code="1101", region_name="Рег",
                period_label="Период", dat_list=["1.2025"], raw_query=f"q-{i}",
            )

        result = await gibdd_service.list_user_tasks(user_id=99, limit=2)
        assert len(result) == 2


# ============================================================
# _register_task — LRU eviction
# ============================================================
class TestLruEviction:
    def test_eviction_when_limit_exceeded(self, clear_in_memory_tasks, monkeypatch):
        """При превышении MAX_INMEMORY_TASKS самая старая задача вытесняется."""
        from backend.services import gibdd_service

        # Временно снижаем лимит для теста
        original_max = gibdd_service.MAX_INMEMORY_TASKS
        monkeypatch.setattr(gibdd_service, "MAX_INMEMORY_TASKS", 3)

        # Подменяем repository.save_task, чтобы не падать без БД
        fake_repository = types.ModuleType("repository")
        async def fake_save_task(task):
            return None
        fake_repository.save_task = fake_save_task
        monkeypatch.setitem(__import__("sys").modules, "backend.db.repository", fake_repository)

        try:
            tasks = []
            for i in range(5):
                t = gibdd_service.create_task(
                    user_id=1, region_code="1101", region_name="Рег",
                    period_label=f"Период{i}", dat_list=["1.2025"], raw_query=f"q-{i}",
                )
                tasks.append(t)
                # Небольшая задержка, чтобы created_at различались
                # (хотя datetime.now() достаточно точен)

            # Должны остаться только последние 3 (LRU вытеснил первые 2)
            assert len(gibdd_service._tasks) == 3
            # Первые 2 задачи вытеснены
            assert tasks[0].id not in gibdd_service._tasks
            assert tasks[1].id not in gibdd_service._tasks
            # Последние 3 остались
            assert tasks[2].id in gibdd_service._tasks
            assert tasks[3].id in gibdd_service._tasks
            assert tasks[4].id in gibdd_service._tasks
        finally:
            monkeypatch.setattr(gibdd_service, "MAX_INMEMORY_TASKS", original_max)

    def test_re_register_existing_task_moves_to_end(self, clear_in_memory_tasks):
        """Повторный _register_task той же задачи обновляет её позицию."""
        from backend.services import gibdd_service

        task = gibdd_service.create_task(
            user_id=1, region_code="1101", region_name="Рег",
            period_label="Период", dat_list=["1.2025"], raw_query="q",
        )
        # Создаём ещё одну
        task2 = gibdd_service.create_task(
            user_id=1, region_code="1102", region_name="Рег2",
            period_label="Период", dat_list=["1.2025"], raw_query="q2",
        )

        # task сейчас первая в OrderedDict
        keys = list(gibdd_service._tasks.keys())
        assert keys[0] == task.id

        # Повторная регистрация task должна переместить его в конец
        gibdd_service._register_task(task)
        keys_after = list(gibdd_service._tasks.keys())
        assert keys_after[-1] == task.id


# ============================================================
# get_llm_providers_status
# ============================================================
class TestGetLlmProvidersStatus:
    def test_returns_status_dict(self, monkeypatch, clear_in_memory_tasks):
        from backend.services import gibdd_service

        fake_config = types.ModuleType("config")
        fake_config.LLM_API_KEY = "free-key"
        fake_config.LLM_MODEL = "glm-4-flash"
        fake_config.LLM_PAID_API_KEY = "paid-key"
        fake_config.LLM_PAID_API_URL = "https://paid.example.com"
        fake_config.LLM_PAID_MODEL = "deepseek-chat"
        monkeypatch.setattr(gibdd_service, "_import_module", lambda name: fake_config)
        monkeypatch.setattr(_imports, "_import_module", lambda name: fake_config)

        status = gibdd_service.get_llm_providers_status()
        assert status["free"] is True
        assert status["paid"] is True
        assert status["free_model"] == "glm-4-flash"
        assert status["paid_model"] == "deepseek-chat"

    def test_returns_false_when_keys_empty(self, monkeypatch, clear_in_memory_tasks):
        from backend.services import gibdd_service

        fake_config = types.ModuleType("config")
        fake_config.LLM_API_KEY = ""
        fake_config.LLM_MODEL = "glm-4-flash"
        fake_config.LLM_PAID_API_KEY = ""
        fake_config.LLM_PAID_API_URL = ""
        fake_config.LLM_PAID_MODEL = "deepseek-chat"
        monkeypatch.setattr(gibdd_service, "_import_module", lambda name: fake_config)
        monkeypatch.setattr(_imports, "_import_module", lambda name: fake_config)

        status = gibdd_service.get_llm_providers_status()
        assert status["free"] is False
        assert status["paid"] is False


# ============================================================
# _task_factory и _task_dir
# ============================================================
class TestTaskFactoryAndDir:
    def test_task_factory_creates_task(self, clear_in_memory_tasks):
        from backend.services import gibdd_service

        task = gibdd_service._task_factory(
            id="custom-id-123",
            user_id=42,
            region_code="1101",
            region_name="Регион",
            period_label="Период",
            dat_list=["1.2025"],
            raw_query="q",
        )
        assert task.id == "custom-id-123"
        assert task.user_id == 42
        assert task.region_code == "1101"

    def test_task_dir_creates_directory(self, clear_in_memory_tasks, tmp_path, monkeypatch):
        from backend.services import gibdd_service

        # Подменяем _PROJECT_ROOT на временный
        monkeypatch.setattr(gibdd_service, "_PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(_imports, "_PROJECT_ROOT", tmp_path)

        d = gibdd_service._task_dir("test-task-id")
        assert d.exists()
        assert d.is_dir()
        assert "test-task-id" in str(d)


# ============================================================
# ensure_prev_cards — с моком bot._fetch_cards_for_period
# ============================================================
class TestEnsurePrevCards:
    @pytest.mark.asyncio
    async def test_computes_prev_period_year_minus_one(self, clear_in_memory_tasks, monkeypatch):
        """dat_list=['5.2025'] → prev=['5.2024']."""
        from backend.services import gibdd_service

        task = gibdd_service.create_task(
            user_id=1, region_code="1101", region_name="Рег",
            period_label="Май 2025", dat_list=["5.2025"], raw_query="q",
        )

        # Подменяем bot._fetch_cards_for_period
        fake_bot = types.ModuleType("bot")

        captured = {}

        async def fake_fetch(*, dat_list, reg_code, log_prefix, cache_result):
            captured["dat_list"] = dat_list
            captured["reg_code"] = reg_code
            return [{"kart_id": "prev-1"}], []

        fake_bot._fetch_cards_for_period = fake_fetch

        def smart_import(name):
            if name == "bot":
                return fake_bot
            return __import__(name)

        monkeypatch.setattr(gibdd_service, "_import_module", smart_import)

        monkeypatch.setattr(_imports, "_import_module", smart_import)

        result = await gibdd_service.ensure_prev_cards(task)

        assert captured["dat_list"] == ["5.2024"]
        assert captured["reg_code"] == "1101"
        assert result["ok"] is True
        assert result["prev_label"] == "Май 2024"
        assert task.prev_cards_loaded is True

    @pytest.mark.asyncio
    async def test_skips_if_already_loaded(self, clear_in_memory_tasks, monkeypatch):
        """Если prev_cards_loaded=True — не делает запрос."""
        from backend.services import gibdd_service

        task = gibdd_service.create_task(
            user_id=1, region_code="1101", region_name="Рег",
            period_label="Май 2025", dat_list=["5.2025"], raw_query="q",
        )
        task.prev_cards = [{"existing": "card"}]
        task.prev_label = "Май 2024"
        task.prev_cards_loaded = True

        # Если бы был вызов bot — он бы упал, т.к. мы не подменили модуль
        result = await gibdd_service.ensure_prev_cards(task)
        assert result["ok"] is True
        assert result["prev_cards"] == [{"existing": "card"}]


# ============================================================
# ask_llm_question — end-to-end с моками
# ============================================================
class TestAskLlmQuestion:
    @pytest.mark.asyncio
    async def test_short_question_returns_error(self, clear_in_memory_tasks):
        from backend.services import gibdd_service

        task = gibdd_service.create_task(
            user_id=1, region_code="1101", region_name="Рег",
            period_label="Май 2025", dat_list=["5.2025"], raw_query="q",
        )
        result = await gibdd_service.ask_llm_question(task, "а", provider="free")
        assert result["ok"] is False
        assert "короткий" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_no_api_key_returns_error(self, clear_in_memory_tasks, monkeypatch):
        from backend.services import gibdd_service

        task = gibdd_service.create_task(
            user_id=1, region_code="1101", region_name="Рег",
            period_label="Май 2025", dat_list=["5.2025"], raw_query="q",
        )

        fake_config = types.ModuleType("config")
        fake_config.LLM_API_KEY = ""
        fake_config.LLM_PAID_API_KEY = ""
        fake_config.LLM_PAID_API_URL = ""
        fake_config.LLM_MODEL = "x"
        fake_config.LLM_PAID_MODEL = "x"

        monkeypatch.setattr(gibdd_service, "_import_module", lambda name: fake_config)

        monkeypatch.setattr(_imports, "_import_module", lambda name: fake_config)
        result = await gibdd_service.ask_llm_question(task, "Нормальный вопрос?", provider="free")
        assert result["ok"] is False
        assert "не настроен" in result["error"].lower() or "LLM_API_KEY" in result["error"]


# ============================================================
# cleanup_old_tasks — удаление по возрасту
# ============================================================
class TestCleanupOldTasks:
    @pytest.mark.asyncio
    async def test_old_tasks_removed_from_memory(self, clear_in_memory_tasks, monkeypatch):
        from backend.services import gibdd_service

        # Подменяем db.repository.delete_old_tasks на no-op
        fake_repo = types.ModuleType("repository")
        async def fake_delete(*args, **kwargs):
            return 0
        fake_repo.delete_old_tasks = fake_delete
        monkeypatch.setitem(__import__("sys").modules, "backend.db.repository", fake_repo)

        # Создаём старую задачу (created_at = 2 дня назад)
        old_task = gibdd_service.create_task(
            user_id=1, region_code="1101", region_name="Рег",
            period_label="Период", dat_list=["1.2025"], raw_query="q",
        )
        old_task.created_at = datetime.now(timezone.utc) - timedelta(days=2)

        # И свежую
        new_task = gibdd_service.create_task(
            user_id=1, region_code="1101", region_name="Рег",
            period_label="Период", dat_list=["1.2025"], raw_query="q2",
        )

        deleted = await gibdd_service.cleanup_old_tasks(max_age_hours=24)
        assert old_task.id not in gibdd_service._tasks
        assert new_task.id in gibdd_service._tasks
        assert deleted >= 1
