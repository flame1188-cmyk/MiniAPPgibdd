"""
Определение версии сборки для cache-busting на клиенте.

Фронтенд раз в 60 сек опрашивает /api/version и сравнивает со
значением VITE_APP_VERSION, встроенным в JS-bundle при сборке.
Если версии не совпадают — значит вышел новый деплой, и пользователю
показывается баннер «Доступна новая версия, обновите страницу».

КРИТИЧНО: backend и frontend должны вычислять версию ОДИНАКОВО.
Иначе после каждого деплоя баннер будет ложно срабатывать.

Приоритет определения версии (одинаковый на backend и при сборке frontend):
1. env APP_BUILD_VERSION — явно задана при деплое
2. env APP_GIT_COMMIT — alias для backward compat
3. Файл версии в стандартных местах (см. _VERSION_FILE_CANDIDATES)
4. git rev-parse --short HEAD — если доступен .git
5. mtime-версия (fallback для контейнеров без git и без файла)

Время сборки:
1. env APP_BUILD_TIME — явно задано
2. Файл build_time.txt (в стандартных местах)
3. Текущее UTC время при старте процесса

Важно: используем ВИДИМЫЕ имена файлов (build_version.txt, не .build_version),
потому что bothost/git/FTP иногда теряют dotfiles при деплое.
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# miniapp/backend/version.py → miniapp/backend → miniapp → корень проекта
# На bothost: /app/miniapp/backend/version.py → parents[2] = /app
# Локально: gibdd-bot/miniapp/backend/version.py → parents[2] = gibdd-bot
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
# miniapp/frontend/dist — где лежит build_version.txt (создаётся build_frontend.sh)
_FRONTEND_DIST = _PROJECT_ROOT / "miniapp" / "frontend" / "dist"

# Стандартные места, где может лежать файл версии.
# Ищем оба варианта: build_version.txt (видимый, рекомендуется) и
# .build_version (dotfile, для обратной совместимости с v2).
#
# КРИТИЧНО: miniapp/frontend/dist/ в .gitignore — значит dist/ может
# не попасть в git-репозиторий. Поэтому дублируем в miniapp/backend/
# (который точно в git) как последнюю линию обороны.
# Dockerfile (multi-stage) копирует /app/build_version.txt через COPY.
# env APP_BUILD_VERSION_FILE — кастомный путь, если все стандартные заняты.
_BACKEND_DIR = _PROJECT_ROOT / "miniapp" / "backend"
_VERSION_FILE_CANDIDATES: list[Path] = [
    _FRONTEND_DIST / "build_version.txt",
    _FRONTEND_DIST / ".build_version",
    _BACKEND_DIR / "BUILD_VERSION.txt",
    _PROJECT_ROOT / "build_version.txt",
    _PROJECT_ROOT / ".build_version",
    Path("/app/build_version.txt"),
    Path("/app/.build_version"),
]
_BUILD_TIME_FILE_CANDIDATES: list[Path] = [
    _FRONTEND_DIST / "build_time.txt",
    _FRONTEND_DIST / ".build_time",
    _BACKEND_DIR / "BUILD_TIME.txt",
    _PROJECT_ROOT / "build_time.txt",
    _PROJECT_ROOT / ".build_time",
    Path("/app/build_time.txt"),
    Path("/app/.build_time"),
]


def _try_file_version() -> str | None:
    """Читает версию из файла.

    Логирует каждый кандидат на INFO уровне — это упрощает диагностику
    на bothost (видно, какие пути проверялись и какой сработал).
    """
    candidates: list[Path] = []
    custom = os.environ.get("APP_BUILD_VERSION_FILE")
    if custom:
        candidates.append(Path(custom))
    candidates.extend(_VERSION_FILE_CANDIDATES)

    for path in candidates:
        try:
            if not path.exists():
                logger.info("[version] file candidate miss: %s", path)
                continue
            version = path.read_text(encoding="utf-8").strip()
            if version and len(version) >= 2:
                logger.info("[version] file candidate HIT: %s → %s", path, version)
                return version
            logger.info("[version] file candidate empty: %s", path)
        except Exception as exc:
            logger.info("[version] file candidate error: %s (%s)", path, exc)
            continue
    return None


def _try_file_build_time() -> str | None:
    """Читает время сборки из файла (те же кандидаты, что и для версии)."""
    candidates: list[Path] = []
    custom = os.environ.get("APP_BUILD_TIME_FILE")
    if custom:
        candidates.append(Path(custom))
    candidates.extend(_BUILD_TIME_FILE_CANDIDATES)

    for path in candidates:
        try:
            if not path.exists():
                continue
            value = path.read_text(encoding="utf-8").strip()
            if value:
                logger.info("[version] build_time HIT: %s", path)
                return value
        except Exception:
            continue
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
            logger.info("[version] git commit: %s", commit)
            return commit
    except Exception:
        pass
    return None


def _try_mtime_version() -> str:
    """Fallback: версия на основе mtime файла main.py.

    ВАЖНО: этот fallback должен быть LAST RESORT — если он сработает
    на backend, но frontend использует git commit, будет рассинхрон
    и ложное срабатывание баннера. Чтобы этого избежать, build_frontend.sh
    всегда пишет build_version.txt рядом с dist/.
    """
    try:
        # Берём mtime main.py (всегда присутствует, обновляется при деплое)
        main_py = _PROJECT_ROOT / "main.py"
        if main_py.exists():
            mtime = int(main_py.stat().st_mtime)
            version = f"local-{mtime:x}"
            logger.info(
                "[version] mtime fallback: %s (main.py mtime=%d)",
                version,
                mtime,
            )
            return version
    except Exception:
        pass
    return "unknown"


def _detect_version() -> str:
    """Возвращает строку версии сборки.

    Приоритет:
    1. env APP_BUILD_VERSION — явно задана (например через docker run -e)
    2. env APP_GIT_COMMIT — alias
    3. Файл build_version.txt (в стандартных местах)
    4. git rev-parse --short HEAD (если доступен .git)
    5. mtime-версия (fallback, last resort)
    """
    # 1. Явно заданная версия через env
    version = (
        os.environ.get("APP_BUILD_VERSION")
        or os.environ.get("APP_GIT_COMMIT")
    )
    if version:
        logger.info("[version] from env APP_BUILD_VERSION: %s", version)
        return version

    # 2. Файл версии (build_frontend.sh или Dockerfile пишет рядом с dist/)
    version = _try_file_version()
    if version:
        return version

    # 3. git commit (если есть .git в проекте)
    version = _try_git_commit()
    if version:
        return version

    # 4. fallback: mtime (last resort — может вызвать рассинхрон с frontend)
    return _try_mtime_version()


def _detect_build_time() -> str:
    """Возвращает ISO-8601 UTC строку времени сборки.

    Приоритет:
    1. env APP_BUILD_TIME
    2. Файл build_time.txt (в стандартных местах)
    3. Текущее время UTC (fallback)
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
