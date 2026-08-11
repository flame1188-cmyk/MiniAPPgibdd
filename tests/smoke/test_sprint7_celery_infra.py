"""
tests/smoke/test_sprint7_celery_infra.py — smoke-тесты для Sprint 7 (вариант C).

Проверяет:
- config.py читает REDIS_URL, CELERY_*, USE_CELERY
- worker/celery_app.py создаёт Celery app с правильными очередями
- worker/tasks/cleanup_tasks.py регистрирует задачи
- Валидация config.py: USE_CELERY=true без REDIS_URL даёт ошибку
- main.py экспортирует /health/redis и /health/celery endpoints

Запуск:
    pytest tests/smoke/test_sprint7_celery_infra.py -v
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ============================================================
# 1. config.py — Sprint 7 переменные
# ============================================================
def test_config_has_redis_url_variable():
    """config.py экспортирует REDIS_URL."""
    import config
    assert hasattr(config, "REDIS_URL")
    assert isinstance(config.REDIS_URL, str)


def test_config_has_celery_variables():
    """config.py экспортирует все CELERY_* переменные."""
    import config
    expected = [
        "CELERY_BROKER_URL",
        "CELERY_RESULT_BACKEND",
        "USE_CELERY",
        "CELERY_WORKER_CONCURRENCY",
        "CELERY_TASK_SOFT_TIME_LIMIT",
        "CELERY_TASK_TIME_LIMIT",
        "CELERY_MAX_TASKS_PER_CHILD",
        "REDIS_PUBSUB_PREFIX",
        "REDIS_TASK_STATE_TTL",
    ]
    for name in expected:
        assert hasattr(config, name), f"config.{name} missing"


def test_config_validate_catches_use_celery_without_redis(monkeypatch):
    """validate_config() возвращает ошибку при USE_CELERY=true и пустом REDIS_URL."""
    import config
    monkeypatch.setattr(config, "USE_CELERY", True)
    monkeypatch.setattr(config, "REDIS_URL", "")
    errors = config.validate_config()
    assert any("REDIS_URL" in e for e in errors), f"Expected REDIS_URL error, got: {errors}"


def test_config_use_celery_defaults_false_without_redis(monkeypatch):
    """USE_CELERY по умолчанию False, если REDIS_URL пустой."""
    import config
    # monkeypatch envs
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("USE_CELERY", raising=False)
    importlib.reload(config)
    assert config.REDIS_URL == ""
    assert config.USE_CELERY is False
    # Восстанавливаем
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("USE_CELERY", "true")
    importlib.reload(config)
    assert config.USE_CELERY is True
    assert config.CELERY_BROKER_URL == "redis://localhost:6379/0/1"
    assert config.CELERY_RESULT_BACKEND == "redis://localhost:6379/0/2"
    # Очищаем env после теста
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("USE_CELERY", raising=False)
    importlib.reload(config)


# ============================================================
# 2. worker/celery_app.py — Celery app
# ============================================================
def test_celery_app_imports():
    """worker.celery_app импортируется без ошибок."""
    from worker.celery_app import app
    assert app.main == "gibdd_worker"


def test_celery_app_has_4_queues():
    """Celery app сконфигурирован с 4 очередями + default."""
    from worker.celery_app import app
    queue_names = list(app.conf.task_queues.keys())
    assert "gibdd" in queue_names
    assert "llm" in queue_names
    assert "clusters" in queue_names
    assert "exports" in queue_names
    assert "celery" in queue_names  # default
    assert len(queue_names) == 5


def test_celery_app_has_beat_schedule():
    """Celery app имеет beat schedule с cleanup задачами."""
    from worker.celery_app import app
    schedule = app.conf.beat_schedule
    assert "cleanup-expired-caches" in schedule
    assert "flush-stale-task-states" in schedule


def test_celery_app_has_time_limits():
    """Celery app имеет soft/hard time limits."""
    from worker.celery_app import app
    assert app.conf.task_soft_time_limit > 0
    assert app.conf.task_time_limit > app.conf.task_soft_time_limit


def test_celery_app_has_acks_late():
    """Celery app использует acks_late для надёжности."""
    from worker.celery_app import app
    assert app.conf.task_acks_late is True
    assert app.conf.task_reject_on_worker_lost is True


def test_celery_ping_worker_returns_dict():
    """ping_worker возвращает dict с ожидаемыми ключами."""
    from worker.celery_app import ping_worker
    result = ping_worker(timeout=0.5)
    assert isinstance(result, dict)
    assert "ok" in result
    assert "workers" in result
    assert "ping_count" in result
    assert "error" in result
    # Без Redis — ok=False и понятная ошибка
    assert result["ok"] is False
    assert "REDIS_URL" in result["error"] or "connect" in result["error"].lower()


# ============================================================
# 3. worker/tasks/cleanup_tasks.py — задачи
# ============================================================
def test_cleanup_tasks_registered():
    """cleanup_expired_caches и flush_stale_task_states зарегистрированы в Celery."""
    from worker.celery_app import app
    from worker.tasks.cleanup_tasks import cleanup_expired_caches, flush_stale_task_states
    assert cleanup_expired_caches.name == "worker.tasks.cleanup_tasks.cleanup_expired_caches"
    assert flush_stale_task_states.name == "worker.tasks.cleanup_tasks.flush_stale_task_states"
    # Зарегистрированы в app
    assert cleanup_expired_caches.name in app.tasks
    assert flush_stale_task_states.name in app.tasks


def test_cleanup_expired_caches_runs_in_eager_mode():
    """cleanup_expired_caches выполняется в eager режиме (без worker) и возвращает stub."""
    from worker.celery_app import app
    from worker.tasks.cleanup_tasks import cleanup_expired_caches

    # Включаем eager mode — задача выполняется синхронно в текущем процессе
    old_value = app.conf.task_always_eager
    app.conf.task_always_eager = True
    try:
        result = cleanup_expired_caches.apply().get(timeout=5)
    finally:
        app.conf.task_always_eager = old_value

    assert isinstance(result, dict)
    assert result.get("stub") is True
    assert "cards_deleted" in result
    assert "clusters_deleted" in result
    assert "excel_deleted" in result
    assert "llm_deleted" in result


# ============================================================
# 4. main.py — health endpoints
# ============================================================
def test_main_has_health_redis_endpoint():
    """main.py экспортирует /health/redis endpoint."""
    try:
        import main
    except Exception as exc:
        pytest.skip(f"main.py не импортируется в этом окружении: {exc}")
    from fastapi.routing import APIRoute
    paths = {r.path for r in main.app.routes if isinstance(r, APIRoute)}
    assert "/health/redis" in paths, f"/health/redis missing. Available: {sorted(paths)}"


def test_main_has_health_celery_endpoint():
    """main.py экспортирует /health/celery endpoint."""
    try:
        import main
    except Exception as exc:
        pytest.skip(f"main.py не импортируется в этом окружении: {exc}")
    from fastapi.routing import APIRoute
    paths = {r.path for r in main.app.routes if isinstance(r, APIRoute)}
    assert "/health/celery" in paths, f"/health/celery missing. Available: {sorted(paths)}"


# ============================================================
# 5. Файлы инфраструктуры существуют
# ============================================================
def test_docker_compose_exists():
    """docker-compose.yml существует для multi-process деплоя."""
    assert (_PROJECT_ROOT / "docker-compose.yml").exists()


def test_dockerfile_worker_exists():
    """Dockerfile.worker существует для Celery worker."""
    assert (_PROJECT_ROOT / "Dockerfile.worker").exists()


def test_worker_package_exists():
    """Пакет worker/ существует с celery_app.py."""
    worker_dir = _PROJECT_ROOT / "worker"
    assert worker_dir.exists()
    assert (worker_dir / "__init__.py").exists()
    assert (worker_dir / "celery_app.py").exists()
    assert (worker_dir / "tasks").is_dir()
    assert (worker_dir / "tasks" / "__init__.py").exists()
    assert (worker_dir / "tasks" / "cleanup_tasks.py").exists()


def test_env_example_has_redis_section():
    """env.example содержит секцию Sprint 7 (Redis + Celery)."""
    content = (_PROJECT_ROOT / "env.example").read_text(encoding="utf-8")
    assert "REDIS_URL" in content
    assert "USE_CELERY" in content
    assert "CELERY_BROKER_URL" in content
    assert "CELERY_RESULT_BACKEND" in content
    assert "Sprint 7" in content or "Celery" in content


def test_requirements_has_celery_and_redis():
    """requirements.txt содержит celery и redis пакеты."""
    content = (_PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "celery" in content.lower()
    assert "redis" in content.lower()
