#!/usr/bin/env bash
# ============================================================
# Сборка Mini App frontend для деплоя на bothost.ru
#
# Запускать ЛОКАЛЬНО (на вашей машине с установленным Node.js):
#   cd /path/to/gibdd-bot
#   bash build_frontend.sh
#
# После сборки загрузите папку miniapp/frontend/dist/
# на bothost вместе с остальным проектом.
#
# Версия сборки (VITE_APP_VERSION) определяется тем же алгоритмом,
# что и на backend (см. miniapp/backend/version.py):
#   1. env APP_BUILD_VERSION (явно)
#   2. env APP_GIT_COMMIT (alias)
#   3. git rev-parse --short HEAD (если есть .git)
#   4. mtime-based fallback: "local-<timestamp>"
# Фронтенд встраивает эту версию в JS-bundle, backend отдаёт через
# /api/version — фронтенд сравнивает и показывает баннер обновления.
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRONTEND_DIR="$SCRIPT_DIR/miniapp/frontend"
DIST_DIR="$FRONTEND_DIR/dist"

# ============================================================
# Определение версии сборки (sync с miniapp/backend/version.py)
# ============================================================
if [ -z "${APP_BUILD_VERSION:-}" ] && [ -z "${APP_GIT_COMMIT:-}" ]; then
    if command -v git >/dev/null 2>&1 && [ -d "$SCRIPT_DIR/.git" ]; then
        APP_BUILD_VERSION="$(git -C "$SCRIPT_DIR" rev-parse --short HEAD 2>/dev/null || echo "")"
    fi
fi
if [ -z "${APP_BUILD_VERSION:-}" ]; then
    APP_BUILD_VERSION="local-$(date +%s)"
fi

# Время сборки (ISO-8601 UTC)
if [ -z "${APP_BUILD_TIME:-}" ]; then
    APP_BUILD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
fi

# Пробрасываем версию в Vite — она встраивается в bundle как
# import.meta.env.VITE_APP_VERSION (используется в useVersionCheck.ts).
export VITE_APP_VERSION="$APP_BUILD_VERSION"
export APP_BUILD_TIME

echo "=== Версия сборки ==="
echo "  VITE_APP_VERSION = $VITE_APP_VERSION"
echo "  APP_BUILD_TIME   = $APP_BUILD_TIME"
echo ""

echo "=== Проверка Node.js ==="
if ! command -v node &>/dev/null; then
    echo "ОШИБКА: Node.js не установлен. Установите с https://nodejs.org (LTS)"
    exit 1
fi
echo "Node.js: $(node --version)"
echo "npm:     $(npm --version)"

echo ""
echo "=== Установка зависимостей ==="
cd "$FRONTEND_DIR"
if [ -f package-lock.json ]; then
    npm ci --no-audit --no-fund
else
    npm install --no-audit --no-fund
fi

echo ""
echo "=== Сборка ==="
npm run build

echo ""
echo "=== Проверка маркеров фикса ==="
if [ -d "$DIST_DIR/assets" ]; then
    # Ищем маркер VITE_APP_VERSION в собранном bundle
    if grep -rE "Доступна новая версия" "$DIST_DIR/assets/" >/dev/null 2>&1; then
        echo "✓ VersionBanner найден в bundle"
    else
        echo "⚠ ВНИМАНИЕ: VersionBanner НЕ найден в bundle — проверьте сборку"
    fi
    if grep -rE "/api/version" "$DIST_DIR/assets/" >/dev/null 2>&1; then
        echo "✓ /api/version endpoint найден в bundle"
    else
        echo "⚠ ВНИМАНИЕ: /api/version НЕ найден в bundle — проверьте сборку"
    fi
    if grep -rE "$VITE_APP_VERSION" "$DIST_DIR/assets/" >/dev/null 2>&1; then
        echo "✓ Версия $VITE_APP_VERSION встроена в bundle"
    else
        echo "⚠ ВНИМАНИЕ: версия $VITE_APP_VERSION НЕ найдена в bundle"
    fi
fi

# ============================================================
# Сохраняем версию в файлы внутри dist/, чтобы backend мог её
# прочитать из того же места после деплоя.
# Файлы едут на bothost вместе с dist/ — синхронно с frontend bundle.
# miniapp/backend/version.py ищет miniapp/frontend/dist/.build_version.
# ============================================================
echo "$VITE_APP_VERSION" > "$DIST_DIR/.build_version"
echo "$APP_BUILD_TIME"   > "$DIST_DIR/.build_time"
echo ""
echo "=== Версия сохранена в dist/.build_version ==="
echo "  version:    $(cat "$DIST_DIR/.build_version")"
echo "  build_time: $(cat "$DIST_DIR/.build_time")"
echo ""
echo "  Backend version.py будет искать этот файл в:"
echo "    miniapp/frontend/dist/.build_version (после деплоя)"
echo "    /app/.build_version (если Dockerfile скопировал отдельно)"

echo ""
echo "=== Готово ==="
if [ -d "$DIST_DIR" ]; then
    SIZE=$(du -sh "$DIST_DIR" | cut -f1)
    echo "Frontend собран в: $DIST_DIR"
    echo "Размер: $SIZE"
    echo ""
    echo "Файлы:"
    ls -lhA "$DIST_DIR"
    echo ""
    echo "Версия: $VITE_APP_VERSION"
    echo "Build time: $APP_BUILD_TIME"
    echo ""
    echo "Теперь загрузите на bothost:"
    echo "  - Весь проект целиком (включая miniapp/frontend/dist/)"
    echo "  - ИЛИ только miniapp/frontend/dist/, если остальной код уже на bothost"
    echo ""
    echo "После деплоя проверьте:"
    echo "  https://<BOTHOST_DOMAIN>/app/"
    echo "  https://<BOTHOST_DOMAIN>/api/version  (должен вернуть ту же версию: $VITE_APP_VERSION)"
else
    echo "ОШИБКА: dist/ не создан"
    exit 1
fi
