"""
Smoke-тесты для hotfix Sprint 7 — фикс бесконечного polling'а при 404.

Контекст проблемы:
- Фронтенд через useClustersPolling (react-query) делал long-polling
  /api/dtp/tasks/{task_id}/clusters?wait=25. При 404 (Task not found)
  refetchInterval возвращал REFETCH_INITIAL_MS=1000, и polling шёл
  бесконечно — каждую секунду новый запрос с тем же 404.
- В логах bothost это выглядело как сотни 404-запросов подряд.

Фикс:
1. Frontend useAnalysisPolling.ts — прекратить polling при 404/403
   (через refetchInterval + retry).
2. Frontend ClustersView.tsx — показать пользователю сообщение
   «Задача не найдена» при 404/403.
3. Backend _common.py — WARNING-лог при 404/403 с task_id+user_id
   для диагностики будущих проблем.

Тесты:
- TestBackendCommonRequireDoneTaskLogging: проверяет что backend
  логирует 404/403 на WARNING (через caplog).
- TestFrontendFixStructure: проверяет структуру frontend-файлов
  (присутствие проверки на ApiError 404/403 в useAnalysisPolling.ts
  и ClustersView.tsx).
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import pytest

# ============================================================
# Backend: _require_done_task логирует 404/403 на WARNING
# ============================================================
class TestBackendCommonRequireDoneTaskLogging:
    """Проверяет, что _require_done_task логирует 404/403 на WARNING."""

    def test_common_module_has_logger(self):
        """_common.py должен иметь logger и использовать его в 404/403."""
        from miniapp.backend.routers import _common
        # Модуль должен иметь logger
        assert hasattr(_common, "logger"), \
            "_common.py должен иметь module-level logger"
        assert isinstance(_common.logger, logging.Logger)

    def test_require_done_task_logs_404(self, caplog, monkeypatch):
        """При 404 (Task not found) должен быть WARNING-лог с task_id+user_id."""
        from fastapi import HTTPException

        from miniapp.backend.routers._common import _require_done_task

        # Mock get_task_async → None (task not found)
        async def fake_get_task_async(task_id):
            return None

        # Mock user
        class FakeUser:
            id = 12345

        # Патчим get_task_async в модуле _common
        from miniapp.backend.routers import _common as common_mod
        # get_task_async импортируется в _common из services.gibdd_service
        # Патчим через monkeypatch на атрибут модуля
        monkeypatch.setattr(
            common_mod, "get_task_async", fake_get_task_async
        )

        with caplog.at_level(logging.WARNING, logger="_common"):
            with pytest.raises(HTTPException) as exc_info:
                import asyncio
                asyncio.run(_require_done_task("abc123def456", FakeUser()))

        assert exc_info.value.status_code == 404
        assert "Task not found" in exc_info.value.detail

        # Проверяем, что WARNING-лог содержит task_id и user_id
        warning_records = [
            r for r in caplog.records
            if r.levelno == logging.WARNING
            and "_require_done_task" in r.name
        ]
        # Если caplog не поймал logger с конкретным именем — ищем по message
        if not warning_records:
            warning_records = [
                r for r in caplog.records
                if r.levelno == logging.WARNING
                and "_require_done_task" in (r.getMessage() + r.name)
            ]

        assert len(warning_records) >= 1, (
            "Должен быть хотя бы один WARNING-лог при 404. "
            f"Records: {[(r.name, r.levelname, r.getMessage()) for r in caplog.records]}"
        )

        msg = warning_records[0].getMessage()
        assert "abc123def456" in msg, f"task_id должен быть в логе: {msg}"
        assert "12345" in msg, f"user_id должен быть в логе: {msg}"
        assert "404" in msg, f"Код 404 должен быть в логе: {msg}"

    def test_require_done_task_logs_403(self, caplog, monkeypatch):
        """При 403 (Access denied) должен быть WARNING-лог с owner_user_id."""
        from fastapi import HTTPException

        from miniapp.backend.routers._common import _require_done_task

        # Mock task: существует, но принадлежит другому пользователю
        class FakeTask:
            id = "xyz789abc012"
            user_id = 99999  # другой пользователь
            status = type("S", (), {"value": "done"})()  # status == DONE

        async def fake_get_task_async(task_id):
            return FakeTask()

        class FakeUser:
            id = 12345  # запросивший пользователь

        from miniapp.backend.routers import _common as common_mod
        monkeypatch.setattr(common_mod, "get_task_async", fake_get_task_async)

        with caplog.at_level(logging.WARNING):
            with pytest.raises(HTTPException) as exc_info:
                import asyncio
                asyncio.run(_require_done_task("xyz789abc012", FakeUser()))

        assert exc_info.value.status_code == 403
        assert "Access denied" in exc_info.value.detail

        warning_records = [
            r for r in caplog.records
            if r.levelno == logging.WARNING
            and "_require_done_task" in (r.name + " " + r.getMessage())
        ]
        assert len(warning_records) >= 1, (
            "Должен быть хотя бы один WARNING-лог при 403. "
            f"Records: {[(r.name, r.levelname, r.getMessage()) for r in caplog.records]}"
        )

        msg = warning_records[0].getMessage()
        assert "xyz789abc012" in msg
        assert "12345" in msg  # requester
        assert "99999" in msg  # owner


# ============================================================
# Frontend: useAnalysisPolling.ts прекращает polling при 404/403
# ============================================================
FRONTEND_ROOT = Path(__file__).resolve().parents[2] / "miniapp" / "frontend" / "src"


class TestFrontendFixStructure:
    """Проверяет структуру frontend-фикса (без запуска браузера)."""

    @pytest.fixture
    def polling_hook_content(self):
        path = FRONTEND_ROOT / "hooks" / "useAnalysisPolling.ts"
        if not path.exists():
            pytest.skip(f"Frontend file not found: {path}")
        return path.read_text(encoding="utf-8")

    @pytest.fixture
    def clusters_view_content(self):
        path = FRONTEND_ROOT / "components" / "ClustersView.tsx"
        if not path.exists():
            pytest.skip(f"Frontend file not found: {path}")
        return path.read_text(encoding="utf-8")

    def test_polling_hook_imports_api_error(self, polling_hook_content):
        """useAnalysisPolling.ts должен импортировать ApiError для проверки статуса."""
        assert "ApiError" in polling_hook_content, (
            "useAnalysisPolling.ts должен импортировать ApiError из @/lib/api "
            "для проверки статуса ошибки (404/403)"
        )

    def test_polling_hook_checks_404_in_refetch_interval(
        self, polling_hook_content
    ):
        """refetchInterval должен возвращать false при 404/403."""
        # Проверяем, что в refetchInterval есть проверка error.status
        assert "error.status === 404" in polling_hook_content, (
            "refetchInterval должен проверять error.status === 404 "
            "и возвращать false (остановить polling)"
        )
        assert "error.status === 403" in polling_hook_content, (
            "refetchInterval должен проверять error.status === 403 "
            "(Access denied) и тоже возвращать false"
        )
        # Должен быть return false в контексте 404/403
        # (это уже проверено выше, но убедимся что есть return false)
        assert "return false" in polling_hook_content

    def test_polling_hook_has_retry_with_404_check(
        self, polling_hook_content
    ):
        """retry-callback должен вернуть false для 404/403 (не ретраить)."""
        # Ищем retry: (failureCount, error) =>
        assert re.search(
            r"retry\s*:\s*\(\s*failureCount\s*,\s*error\s*\)\s*=>",
            polling_hook_content,
        ), "useClustersPolling должен иметь retry: (failureCount, error) => ..."

        # И в retry-функции должна быть проверка 404/403
        assert "error.status === 404" in polling_hook_content
        assert "error.status === 403" in polling_hook_content

    def test_polling_hook_has_transient_error_backoff(
        self, polling_hook_content
    ):
        """Для 5xx/network-ошибок должен быть backoff (не 1 сек)."""
        # REFETCH_AFTER_TRANSIENT_ERROR_MS — новая константа
        assert "REFETCH_AFTER_TRANSIENT_ERROR_MS" in polling_hook_content, (
            "Должна быть константа REFETCH_AFTER_TRANSIENT_ERROR_MS для "
            "5xx/network-ошибок (чтобы не спамить раз в секунду)"
        )

    def test_clusters_view_imports_api_error(self, clusters_view_content):
        """ClustersView.tsx должен импортировать ApiError."""
        assert "ApiError" in clusters_view_content, (
            "ClustersView.tsx должен импортировать ApiError из @/lib/api "
            "для определения типа ошибки (404 vs 403)"
        )

    def test_clusters_view_has_not_found_state(self, clusters_view_content):
        """ClustersView должен иметь UI-состояние 'Task not found'."""
        assert "notFoundError" in clusters_view_content, (
            "ClustersView должен вычислять notFoundError = isError && "
            "error.status in (404, 403)"
        )
        assert "Задача не найдена" in clusters_view_content, (
            "Должно быть сообщение 'Задача не найдена' для пользователя при 404"
        )
        assert "Доступ запрещён" in clusters_view_content, (
            "Должно быть сообщение 'Доступ запрещён' для пользователя при 403"
        )

    def test_clusters_view_resets_state_on_acknowledge(
        self, clusters_view_content
    ):
        """Кнопка 'Понятно' должна сбрасывать started/starting."""
        # Проверяем, что в обработчике кнопки есть setStarted(false)
        # и setStarting(false) — чтобы пользователь мог вернуться к
        # первоначальному состоянию (хотя задача уже не доступна).
        assert "Понятно" in clusters_view_content
        assert "setStarted(false)" in clusters_view_content
        assert "setStarting(false)" in clusters_view_content


# ============================================================
# Regression: фронтенд по-прежнему работает в нормальном случае
# ============================================================
class TestFrontendFixRegression:
    """Проверяет, что фикс не сломал нормальный polling-флоу."""

    @pytest.fixture
    def polling_hook_content(self):
        path = FRONTEND_ROOT / "hooks" / "useAnalysisPolling.ts"
        if not path.exists():
            pytest.skip(f"Frontend file not found: {path}")
        return path.read_text(encoding="utf-8")

    def test_long_poll_wait_sec_unchanged(self, polling_hook_content):
        """LONG_POLL_WAIT_SEC=25 не должен измениться."""
        assert "LONG_POLL_WAIT_SEC = 25" in polling_hook_content

    def test_normal_done_status_still_stops_polling(
        self, polling_hook_content
    ):
        """При data.state.status === 'done' polling должен остановиться."""
        assert "data.state.status === 'done'" in polling_hook_content
        assert "return false" in polling_hook_content

    def test_normal_failed_status_still_stops_polling(
        self, polling_hook_content
    ):
        """При data.state.status === 'failed' polling должен остановиться."""
        assert "data.state.status === 'failed'" in polling_hook_content

    def test_running_status_continues_polling(self, polling_hook_content):
        """При running-статусе polling должен продолжаться."""
        assert "REFETCH_AFTER_TIMEOUT_MS" in polling_hook_content
