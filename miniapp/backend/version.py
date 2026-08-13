"""
Определение версии сборки для cache-busting на клиенте.

Фронтенд раз в 60 сек опрашивает /api/version и сравнивает со
значением VITE_APP_VERSION, встроенным в JS-bundle при сборке.
Если версии не совпадают — значит вышел новый деплой, и пользователю
показывается баннер «Доступна новая версия, обновите страницу».

Приоритет определения версии (одинаковый на backend и при сборке frontend):
1. env APP_BUILD_VERSION — явно задана при деплое
2. env APP_GIT_COMMIT    — alias дляbackward compat
3. git rev-parse --short HEAD — если доступен .git
4. mtime of version.py   — fallback для контейнеров без git

Время сборки:
1. env APP_BUILD_TIME    — явно задано
2. текущее UTC время при старте процесса
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# miniapp/backend/version.py → miniapp/backend → miniapp → gibdd-bot
_PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _try_file_version() -> str | None:
    """Читает версию из файла (создаётся Docker build stage).

    В Dockerfile мы сохраняем VERSION.txt из build-stage в /app/.build_version
    и указываем путь через env APP_BUILD_VERSION_FILE. Это нужно, потому что
    ENV нельзя установить из содержимого файла в Dockerfile, альтернативы:
      - читать файл при старте процесса (этот метод)
      - запускать entrypoint.sh, который экспортирует env из файла
    Первый вариант проще и не зависит от shell-логики.
    """
    path = os.environ.get("APP_BUILD_VERSION_FILE")
    if not path:
        return None
    try:
        p = Path(path)
        if not p.exists():
            return None
        version = p.read_text(encoding="utf-8").strip()
        if version and len(version) >= 2:
            return version
    except Exception:
        pass
    return None


def _try_file_build_time() -> str | None:
    """Читает время сборки из файла (Docker build stage)."""
    path = os.environ.get("APP_BUILD_TIME_FILE")
    if not path:
        return None
    try:
        p = Path(path)
        if not p.exists():
            return None
        value = p.read_text(encoding="utf-8").strip()
        if value:
            return value
    except Exception:
        pass
    return None


def _try_git_commit() -> str | None:
    """Пытается получить short hash текущего git коммита."""
    try:
        result = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(_PROJECT_ROOT),
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        commit = result.decode("utf-8", errors="ignore").strip()
        if commit and len(commit) >= 4:
            return commit
    except Exception:
        pass
    return None


def _try_mtime_version() -> str:
    """Fallback: версия на основе mtime файла version.py."""
    try:
        mtime = int(_PROJECT_ROOT.stat().st_mtime)
        return f"local-{mtime:x}"
    except Exception:
        return "unknown"


def _detect_version() -> str:
    """Возвращает строку версии сборки.

    Приоритет:
    1. env APP_BUILD_VERSION — явно задана (например через docker run -e)
    2. env APP_GIT_COMMIT — alias
    3. Файл APP_BUILD_VERSION_FILE (Docker build stage — version.txt)
    4. git rev-parse --short HEAD (если доступен .git)
    5. mtime-версия (fallback для локальной разработки без git)
    """
    # 1. Явно заданная версия через env (рекомендуется для Docker/bothost)
    version = (
        os.environ.get("APP_BUILD_VERSION")
        or os.environ.get("APP_GIT_COMMIT")
    )
    if version:
        return version

    # 2. Файл из Docker build stage (содержит то, что было вычислено в build-frontend)
    version = _try_file_version()
    if version:
        return version

    # 3. git commit (если есть .git в проекте)
    version = _try_git_commit()
    if version:
        return version

    # 4. fallback: mtime
    return _try_mtime_version()


def _detect_build_time() -> str:
    """Возвращает ISO-8601 UTC строку времени сборки.

    Приоритет:
    1. env APP_BUILD_TIME
    2. Файл APP_BUILD_TIME_FILE (Docker build stage)
    3. Текущее время UTC (fallback для локальной разработки)
    """
    build_time = os.environ.get("APP_BUILD_TIME")
    if build_time:
        return build_time

    build_time = _try_file_build_time()
    if build_time:
        return build_time

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# Вычисляем один раз при импорте модуля — это значение живёт
# весь lifetime процесса и не меняется между запросами.
VERSION: str = _detect_version()
BUILD_TIME: str = _detect_build_time()

logger.info(
    "Build version: %s (build_time=%s, root=%s)",
    VERSION,
    BUILD_TIME,
    _PROJECT_ROOT,
)
