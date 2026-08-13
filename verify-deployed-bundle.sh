#!/usr/bin/env bash
# verify-deployed-bundle.sh — проверка, что задеплоенный dist/ содержит hotfix.
#
# Запускать на bothost:
#   bash verify-deployed-bundle.sh /app/miniapp/frontend/dist
#
# Exit codes:
#   0 — все проверки пройдены, фикс на месте
#   1 —Dist не содержит фикса, деплой некорректен

set -euo pipefail

DIST_DIR="${1:-/app/miniapp/frontend/dist}"

echo "=== Проверка dist/ на наличие Sprint 7 Hotfix ==="
echo "Путь: $DIST_DIR"
echo ""

# 1. Проверить существование dist/
if [ ! -d "$DIST_DIR" ]; then
    echo "❌ FAIL: dist/ не существует по пути $DIST_DIR"
    exit 1
fi

# 2. Найти main JS bundle
MAIN_JS=$(ls "$DIST_DIR"/assets/index-*.js 2>/dev/null | head -1)
if [ -z "$MAIN_JS" ]; then
    echo "❌ FAIL: не найден assets/index-*.js в dist/"
    exit 1
fi
echo "Main bundle: $(basename "$MAIN_JS")"
echo ""

# 3. Проверить маркеры фикса
PASS=0
FAIL=0

check() {
    local desc="$1"
    local pattern="$2"
    local expected_min="${3:-1}"
    local count
    count=$(grep -oE "$pattern" "$MAIN_JS" | wc -l)
    if [ "$count" -ge "$expected_min" ]; then
        echo "✓ $desc ($count совпадений)"
        PASS=$((PASS+1))
    else
        echo "❌ $desc (ожидалось >=$expected_min, найдено $count)"
        FAIL=$((FAIL+1))
    fi
}

check "UI: 'Задача не найдена'" "Задача не найдена" 1
check "UI: 'Доступ запрещён'" "Доступ запрещён" 1
check "Логика: проверка status===404" "status===404" 2
check "Логика: проверка status===403" "status===403" 2

echo ""
echo "=== Backend: _common.py WARNING-лог ==="
BACKEND_COMMON="/app/miniapp/backend/routers/_common.py"
if [ -f "$BACKEND_COMMON" ]; then
    if grep -q "_require_done_task: 404" "$BACKEND_COMMON"; then
        echo "✓ _common.py содержит WARNING-лог при 404"
        PASS=$((PASS+1))
    else
        echo "❌ _common.py НЕ содержит WARNING-лог при 404"
        FAIL=$((FAIL+1))
    fi
else
    echo "? $BACKEND_COMMON не найден — пропускаем backend-проверку"
fi

echo ""
echo "=== Итог ==="
echo "PASS: $PASS"
echo "FAIL: $FAIL"

if [ "$FAIL" -gt 0 ]; then
    echo ""
    echo "❌ Деплой НЕ корректен — нужно пересобрать/перезалить dist/"
    exit 1
else
    echo ""
    echo "✅ Все маркеры фикса на месте. Деплой корректен."
    exit 0
fi
