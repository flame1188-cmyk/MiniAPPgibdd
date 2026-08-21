"""
Smoke-тесты для hotfix Sprint 7 — гарантированное сохранение задачи в БД.

Контекст: пользователь сообщил, что задача f5929f37ee01 (которую фронтенд
бесконечно опрашивал через /clusters?wait=25 с 404) отсутствует в таблице
tasks БД. Это значит, что задача НИКОГДА не была сохранена в БД —
create_task использовал fire-and-forget через asyncio.create_task(save_task),
и корутина не успела выполниться до рестарта контейнера.

Тесты покрывают 4 аспекта фикса:
1. create_task в pipeline.py — синхронное обновление _TASKS_MEMORY + done-callback
2. create_dtp_task в dtp.py — await save_task(task) после create_task
3. execute_task exception-handler в pipeline.py — await save_task для FAILED
4. lifespan shutdown в main.py — persist всех _tasks в БД перед закрытием пула
"""
from __future__ import annotations

import ast
import asyncio
import logging
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MAIN_PY = PROJECT_ROOT / "main.py"
PIPELINE_PY = PROJECT_ROOT / "miniapp" / "backend" / "services" / "pipeline.py"
DTP_PY = PROJECT_ROOT / "miniapp" / "backend" / "routers" / "dtp.py"


# ============================================================
# 1. pipeline.create_task — синхронное обновление _TASKS_MEMORY + callback
# ============================================================
class TestCreateTaskPersistsImmediately:
    """Проверяет, что create_task синхронно обновляет _TASKS_MEMORY."""

    def test_create_task_registers_in_task_registry(self):
        """create_task должен зарегистрировать task через _register_task()."""
        source = PIPELINE_PY.read_text(encoding="utf-8")
        # _register_task(task) вызывается синхронно — это единственный
        # in-memory кэш задач (consolidation: _TASKS_MEMORY удалён).
        assert re.search(
            r"_register_task\(task\)",
            source,
        ), (
            "create_task должен вызывать _register_task(task) — "
            "это помещает задачу в task_registry._tasks (единственный "
            "in-memory кэш). Раньше также писалось в _TASKS_MEMORY, "
            "но этот дублирующий кэш удалён при консолидации."
        )

    def test_create_task_has_done_callback(self):
        """create_task должен добавлять done-callback к save_task future."""
        source = PIPELINE_PY.read_text(encoding="utf-8")
        assert "add_done_callback" in source, (
            "create_task должен вызывать fut.add_done_callback(...) для "
            "логирования ошибок save_task. Раньше asyncio.create_task "
            "молча глотал исключения."
        )

    def test_make_save_task_callback_exists(self):
        """Должна быть функция _make_save_task_callback для логирования ошибок."""
        source = PIPELINE_PY.read_text(encoding="utf-8")
        assert "def _make_save_task_callback" in source, (
            "Должна быть функция _make_save_task_callback(task_id), "
            "возвращающая done-callback"
        )

    def test_make_save_task_callback_logs_warnings(self):
        """Callback должен логировать WARNING при ошибках save_task."""
        source = PIPELINE_PY.read_text(encoding="utf-8")
        # Извлекаем тело _make_save_task_callback
        match = re.search(
            r"def _make_save_task_callback\(task_id[^)]*\).*?(?=\n\ndef |\nclass |\Z)",
            source,
            re.DOTALL,
        )
        assert match, "Не найдена функция _make_save_task_callback"
        body = match.group(0)
        assert "logger.warning" in body, (
            "Callback должен логировать WARNING при ошибках save_task"
        )
        assert "CancelledError" in body, (
            "Callback должен корректно обрабатывать CancelledError "
            "(нормальное завершение при shutdown)"
        )

    def test_create_task_still_returns_task(self):
        """create_task по-прежнему возвращает Task (синхронная, не async)."""
        from miniapp.backend.services.pipeline import create_task

        # create_task должна быть синхронной (не coroutinified)
        assert not asyncio.iscoroutinefunction(create_task), (
            "create_task должен оставаться синхронной — его вызывают из "
            "тестов и других синхронных мест. Менять на async — ломающий change."
        )

        # Создаём task с замоканным _register_task и save_task.
        # Важно: импортируем repository ЯВНО, чтобы patch сработал.
        from miniapp.backend.db import repository  # noqa: F401

        # Создаём task с замоканным _register_task и save_task
        with patch("miniapp.backend.services.pipeline._register_task"):
                # asyncio.create_task требует running loop
                async def _run():
                    with patch(
                        "miniapp.backend.db.repository.save_task",
                        new=AsyncMock(),
                    ):
                        task = create_task(
                            user_id=123,
                            region_code="1101",
                            region_name="Тест",
                            period_label="Май 2025",
                            dat_list=["5.2025"],
                            raw_query="q",
                        )
                        return task

                task = asyncio.run(_run())
                assert task is not None
                assert task.user_id == 123
                assert task.region_code == "1101"


# ============================================================
# 2. dtp.py — await save_task после create_task
# ============================================================
class TestDtpRouterPersistsTaskBeforeExecute:
    """Проверяет, что router dtp.py persist'ит task в БД до execute_task."""

    def test_create_dtp_task_awaits_save_task(self):
        """create_dtp_task должен await save_task(task) после create_task."""
        source = DTP_PY.read_text(encoding="utf-8")
        # Ищем await save_task(task) в теле create_dtp_task
        # между create_task(...) и asyncio_create_task(task.id)
        match = re.search(
            r"task\s*=\s*create_task\([^)]*\).*?asyncio_create_task\(task\.id\)",
            source,
            re.DOTALL,
        )
        assert match, "Не найден блок между create_task и asyncio_create_task"
        block = match.group(0)
        assert "await save_task(task)" in block, (
            "create_dtp_task должен вызывать await save_task(task) между "
            "create_task() и asyncio_create_task(task.id) — это гарантирует "
            "что метаданные задачи в БД до запуска execute_task"
        )

    def test_create_dtp_task_logs_persist_failure(self):
        """Если save_task падает — должен быть WARNING-лог."""
        source = DTP_PY.read_text(encoding="utf-8")
        # Ищем try/except вокруг await save_task
        assert re.search(
            r"try:\s*from\s+\.\.db\.repository\s+import\s+save_task\s+"
            r"await\s+save_task\(task\)\s*except\s+Exception.*?warning",
            source,
            re.DOTALL | re.IGNORECASE,
        ), (
            "create_dtp_task должен оборачивать await save_task в try/except "
            "и логировать WARNING при ошибке (не роняя endpoint)"
        )


# ============================================================
# 3. pipeline.execute_task exception-handler — persist FAILED-статуса
# ============================================================
class TestExecuteTaskPersistsFailedStatus:
    """Проверяет, что execute_task persist'ит FAILED-статус в БД."""

    def test_execute_task_outer_exception_handler_calls_save_task(self):
        """Внешний except-блок execute_task должен вызывать await save_task."""
        source = PIPELINE_PY.read_text(encoding="utf-8")
        # Ищем внешний exception-handler (после semaphore-wrapped лога).
        # Берём большой контекст: от "semaphore-wrapped" до конца exception-блока.
        match = re.search(
            r'logger\.exception\(f"Task\s+\{task_id\}\s+failed\s+'
            r'\(semaphore-wrapped\)"\).*?(?=\n\n\n|\nasync def |\nclass )',
            source,
            re.DOTALL,
        )
        assert match, (
            "Не найден внешний exception-handler execute_task "
            "(semaphore-wrapped)"
        )
        block = match.group(0)
        assert "await save_task(task)" in block, (
            "Внешний exception-handler execute_task должен вызывать "
            "await save_task(task) — иначе FAILED-статус теряется в in-memory"
        )

    def test_execute_task_persist_failure_does_not_raise(self):
        """Если save_task в exception-handler падает — не должен ронять pipeline."""
        source = PIPELINE_PY.read_text(encoding="utf-8")
        # Ищем try/except вокруг save_task в exception-handler
        match = re.search(
            r"try:\s*from\s+\.\.db\.repository\s+import\s+save_task\s+"
            r"await\s+save_task\(task\)\s*except\s+Exception\s+as\s+"
            r"persist_exc:\s*logger\.warning",
            source,
            re.DOTALL,
        )
        assert match, (
            "save_task в exception-handler должен быть в try/except с "
            "WARNING-логом — иначе двойная ошибка (pipeline + persist) "
            "приведёт к Unhandled exception"
        )


# ============================================================
# 4. main.py lifespan shutdown — persist всех _tasks в БД
# ============================================================
class TestLifespanShutdownPersistsTasks:
    """Проверяет, что lifespan shutdown persist'ит все _tasks в БД."""

    def test_lifespan_has_shutdown_persist_block(self):
        """В lifespan должен быть блок persist _tasks перед db_close_pool."""
        source = MAIN_PY.read_text(encoding="utf-8")
        # Ищем shutdown-persist блок
        assert "Shutdown: persist" in source, (
            "В lifespan shutdown должен быть блок с логом "
            "'Shutdown: persist N in-memory задач в БД перед закрытием пула'"
        )

    def test_lifespan_shutdown_persists_before_db_close(self):
        """Persist должен быть ДО db_close_pool, иначе соединения уже закрыты."""
        source = MAIN_PY.read_text(encoding="utf-8")
        persist_pos = source.find("Shutdown: persist")
        close_pos = source.find("db_close_pool()")
        assert persist_pos > 0, "Не найден persist-блок в shutdown"
        assert close_pos > 0, "Не найден db_close_pool в shutdown"
        assert persist_pos < close_pos, (
            "Persist всех _tasks должен быть ДО db_close_pool — иначе "
            "соединения уже закрыты и save_task упадёт"
        )

    def test_lifespan_shutdown_checks_db_ready(self):
        """Persist должен проверять is_db_ready() — иначе упадёт без БД."""
        source = MAIN_PY.read_text(encoding="utf-8")
        # Ищем is_db_ready() в shutdown-блоке
        shutdown_start = source.find("Graceful shutdown")
        assert shutdown_start > 0
        shutdown_end = source.find("Закрываем пул PostgreSQL")
        assert shutdown_end > shutdown_start
        shutdown_block = source[shutdown_start:shutdown_end]
        assert "is_db_ready" in shutdown_block, (
            "Persist-блок shutdown должен проверять is_db_ready() — "
            "иначе save_task упадёт при запуске без DATABASE_URL"
        )

    def test_lifespan_shutdown_handles_persist_errors(self):
        """Ошибки persist отдельных задач не должны ронять shutdown."""
        source = MAIN_PY.read_text(encoding="utf-8")
        # Ищем try/except вокруг save_task в shutdown
        assert re.search(
            r"try:\s*await\s+save_task\(t\).*?except\s+Exception\s+as\s+exc:\s*"
            r"logger\.warning.*?persist\s+task_id",
            source,
            re.DOTALL,
        ), (
            "В shutdown persist каждой задачи должен быть в try/except — "
            "иначе одна битая задача роняет весь shutdown"
        )

    def test_lifespan_shutdown_logs_summary(self):
        """Должен быть итоговый лог 'persisted N/M задач в БД'."""
        source = MAIN_PY.read_text(encoding="utf-8")
        assert "persisted" in source and "задач в БД" in source, (
            "Должен быть лог 'Shutdown: persisted N/M задач в БД' для "
            "мониторинга качества shutdown"
        )


# ============================================================
# 5. Регрессия: create_task по-прежнему работает в нормальном сценарии
# ============================================================
class TestCreateTaskRegression:
    """Проверяет, что фикс не сломал существующий create_task contract."""

    def test_create_task_signature_unchanged(self):
        """Сигнатура create_task должна остаться прежней."""
        from miniapp.backend.services.pipeline import create_task
        import inspect

        sig = inspect.signature(create_task)
        params = list(sig.parameters.keys())
        assert params == [
            "user_id", "region_code", "region_name",
            "period_label", "dat_list", "raw_query",
        ], f"Сигнатра create_task изменилась: {params}"

    def test_create_task_returns_task_with_id(self):
        """create_task должен возвращать Task с заполненным id."""
        from miniapp.backend.services.pipeline import create_task
        from miniapp.backend.db import repository  # noqa: F401 — нужен для patch

        with patch("miniapp.backend.services.pipeline._register_task"):
                async def _run():
                    with patch(
                        "miniapp.backend.db.repository.save_task",
                        new=AsyncMock(),
                    ):
                        task = create_task(
                            user_id=1, region_code="1101",
                            region_name="Рег", period_label="Май 2025",
                            dat_list=["5.2025"], raw_query="q",
                        )
                        return task
                task = asyncio.run(_run())
                assert task.id is not None
                assert len(task.id) == 12  # _gen_task_id возвращает 12 hex


# ============================================================
# 6. Интеграционный тест: сценарий из баг-репорта
# ============================================================
class TestBugReportScenario:
    """Воспроизводит сценарий: задача создана, контейнер перезапущен,
    фронтенд опрашивает /clusters → должен быть найден в БД (не 404).
    """

    def test_create_task_then_save_task_called(self):
        """После create_task save_task должен быть вызван (через asyncio.create_task)."""
        from miniapp.backend.services.pipeline import create_task
        from miniapp.backend.db import repository  # noqa: F401 — нужен для patch

        # Замокаем save_task и убедимся, что asyncio.create_task вызывает его
        save_task_mock = AsyncMock()

        with patch("miniapp.backend.services.pipeline._register_task"):
                async def _run():
                    with patch(
                        "miniapp.backend.db.repository.save_task",
                        new=save_task_mock,
                    ):
                        task = create_task(
                            user_id=1, region_code="1101",
                            region_name="Рег", period_label="Май 2025",
                            dat_list=["5.2025"], raw_query="q",
                        )
                        # Даём event loop'у выполнить asyncio.create_task
                        await asyncio.sleep(0.01)
                        return task

                task = asyncio.run(_run())
                # save_task должен быть вызван
                assert save_task_mock.called, (
                    "save_task должен быть вызван через asyncio.create_task "
                    f"после create_task (task_id={task.id})"
                )

    def test_create_dtp_task_endpoint_persists_before_returning(self):
        """POST /dtp/tasks должен await'ить save_task до возврата ответа.

        Это гарантирует, что к моменту когда фронтенд получает task_id,
        задача уже в БД — даже если execute_task упадёт в самом начале.
        """
        source = DTP_PY.read_text(encoding="utf-8")
        # Ищем: после await save_task(task) идёт return TaskCreateResponse
        # (через asyncio_create_task(task.id) и return)
        match = re.search(
            r"await\s+save_task\(task\).*?"
            r"asyncio_create_task\(task\.id\).*?"
            r"return\s+TaskCreateResponse",
            source,
            re.DOTALL,
        )
        assert match, (
            "Порядок должен быть: await save_task(task) → asyncio_create_task "
            "→ return TaskCreateResponse. Если return идёт до save_task, "
            "фронтенд может получить task_id раньше, чем задача в БД."
        )
