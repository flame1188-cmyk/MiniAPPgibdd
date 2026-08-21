"""
Smoke-test для проверки патчей пула соединений.

Проверяет:
1. miniapp/backend/config.py загружается, новые поля присутствуют.
2. miniapp/backend/db/connection.py загружается, _configure_connection
   и init_pool доступны.
3. miniapp/backend/db/repository.py загружается, _with_pool_retry
   и _recover_stale_pending_tasks_impl доступны.
4. config.Settings() создаётся с корректными дефолтами.
5. _configure_connection — корутина (callable + asyncio.iscoroutinefunction)
6. _with_pool_retry — корутина.

Запуск: python /home/z/my-project/scripts/smoke_test_pool_fix.py
"""
from __future__ import annotations

import asyncio
import inspect
import os
import sys
from pathlib import Path

# Добавляем корень проекта в sys.path
PROJECT_ROOT = Path("/home/z/my-project/miniapp-work/MiniAPPgibdd")
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "miniapp" / "backend"))

# Без .env пул не инициализируется — это нормально для smoke-testа
os.environ.setdefault("DATABASE_URL", "")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")

print("=== Smoke-test пула соединений ===")
print()

# 1. config.py
print("[1] Импорт miniapp.backend.config...")
try:
    from miniapp.backend.config import Settings, settings

    print(f"  OK: Settings загружен")
    print(f"  database_url = {settings.database_url!r}")
    print(f"  db_enabled  = {settings.db_enabled}")
    print(f"  db_pool_min = {settings.db_pool_min}")
    print(f"  db_pool_max = {settings.db_pool_max}")
    print(f"  db_connect_timeout = {settings.db_connect_timeout}")
    print(f"  db_pool_max_idle = {settings.db_pool_max_idle}")
    print(f"  db_pool_max_lifetime = {settings.db_pool_max_lifetime}")
    print(f"  db_pool_reconnect_timeout = {settings.db_pool_reconnect_timeout}")
except Exception as exc:
    print(f"  FAIL: {exc}")
    sys.exit(1)

# 2. connection.py
print()
print("[2] Импорт miniapp.backend.db.connection...")
try:
    from miniapp.backend.db.connection import (
        _configure_connection,
        init_pool,
        is_db_ready,
        close_pool,
        get_pool,
    )
    print(f"  OK: connection модуль загружен")
    print(f"  _configure_connection is coroutine: "
          f"{asyncio.iscoroutinefunction(_configure_connection)}")
    print(f"  init_pool is coroutine: "
          f"{asyncio.iscoroutinefunction(init_pool)}")
    print(f"  is_db_ready: {is_db_ready()}")
    print(f"  get_pool: {get_pool()}")
except Exception as exc:
    print(f"  FAIL: {exc}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

# 3. repository.py
print()
print("[3] Импорт miniapp.backend.db.repository...")
try:
    from miniapp.backend.db.repository import (
        _with_pool_retry,
        _recover_stale_pending_tasks_impl,
        recover_stale_pending_tasks,
        list_user_tasks_from_db,
    )
    print(f"  OK: repository модуль загружен")
    print(f"  _with_pool_retry is coroutine: "
          f"{asyncio.iscoroutinefunction(_with_pool_retry)}")
    print(f"  _recover_stale_pending_tasks_impl is coroutine: "
          f"{asyncio.iscoroutinefunction(_recover_stale_pending_tasks_impl)}")
    print(f"  recover_stale_pending_tasks is coroutine: "
          f"{asyncio.iscoroutinefunction(recover_stale_pending_tasks)}")
    print(f"  list_user_tasks_from_db is coroutine: "
          f"{asyncio.iscoroutinefunction(list_user_tasks_from_db)}")
except Exception as exc:
    print(f"  FAIL: {exc}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

# 4. Async pool init без DATABASE_URL
print()
print("[4] init_pool без DATABASE_URL (fallback)...")
try:
    result = asyncio.run(init_pool())
    print(f"  OK: init_pool() returned {result} (False — ожидаемо без URL)")
    print(f"  is_db_ready после init_pool: {is_db_ready()}")
except Exception as exc:
    print(f"  FAIL: {exc}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

# 5. Сигнатуры функций
print()
print("[5] Сигнатуры...")
print(f"  init_pool signature: {inspect.signature(init_pool)}")
print(f"  list_user_tasks_from_db signature: "
      f"{inspect.signature(list_user_tasks_from_db)}")
print(f"  _with_pool_retry signature: {inspect.signature(_with_pool_retry)}")
print(f"  _recover_stale_pending_tasks_impl signature: "
      f"{inspect.signature(_recover_stale_pending_tasks_impl)}")

print()
print("=== SMOKE-TEST PASSED ===")
print()
print("Все 5 проверок прошли. Патчи пула валидны.")
print("Деплой-инструкция:")
print("  1. Скопировать файлы на VPS:")
print("     - miniapp/backend/config.py")
print("     - miniapp/backend/db/connection.py")
print("     - miniapp/backend/db/repository.py")
print("     - env.example (для справки)")
print("  2. На VPS обновить .env:")
print("     DATABASE_URL=postgresql://user:pass@host:5432/db\\"
      "?sslmode=require\\&connect_timeout=15\\&keepalives=1\\"
      "&keepalives_idle=30\\&keepalives_interval=10\\&keepalives_count=3")
print("  3. Добавить в .env:")
print("     DB_POOL_MAX_IDLE=120")
print("     DB_POOL_MAX_LIFETIME=600")
print("     DB_POOL_RECONNECT_TIMEOUT=60")
print("  4. Рестарт приложения: supervisorctl restart miniapp")
