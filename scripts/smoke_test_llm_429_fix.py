"""
Smoke-test для проверки патча per-provider LLM 429-cooldown.

Проверяет:
1. llm_analyzer.py импортируется без ошибок
2. mark_429 и wait_429_cooldown принимают provider параметр
3. _llm_429_until — dict[str, float], не float
4. _do_llm_request и _do_llm_stream_request принимают provider параметр
5. _ask_llm_stream_free/_ask_llm_stream_paid передают provider
6. deepseek-v4-flash есть в pricing table
7. Изоляция: 429 от free не активирует cooldown для paid

Запуск: python /home/z/my-project/scripts/smoke_test_llm_429_fix.py
"""
from __future__ import annotations

import asyncio
import inspect
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path("/home/z/my-project/miniapp-work/MiniAPPgibdd")
sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("DATABASE_URL", "")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")
os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_PAID_API_KEY", "test-paid-key")

print("=== Smoke-test: LLM per-provider 429-cooldown ===")
print()

# 1. Импорт llm_analyzer
print("[1] Импорт llm_analyzer...")
try:
    import llm_analyzer
    print("  OK: llm_analyzer импортирован")
except Exception as exc:
    print(f"  FAIL: {exc}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

# 2. _llm_429_until — dict, не float
print()
print("[2] Проверка структуры _llm_429_until...")
try:
    from llm_analyzer import _llm_429_until
    print(f"  type: {type(_llm_429_until).__name__}")
    assert isinstance(_llm_429_until, dict), (
        f"Ожидался dict, получен {type(_llm_429_until).__name__}"
    )
    assert "free" in _llm_429_until, "Нет ключа 'free'"
    assert "paid" in _llm_429_until, "Нет ключа 'paid'"
    print(f"  free: {_llm_429_until['free']}")
    print(f"  paid: {_llm_429_until['paid']}")
    print("  OK: per-provider структура корректна")
except AssertionError as exc:
    print(f"  FAIL: {exc}")
    sys.exit(1)
except Exception as exc:
    print(f"  FAIL: {exc}")
    sys.exit(1)

# 3. Сигнатуры mark_429 и wait_429_cooldown
print()
print("[3] Сигнатуры mark_429 и wait_429_cooldown...")
try:
    sig_mark = inspect.signature(llm_analyzer.mark_429)
    sig_wait = inspect.signature(llm_analyzer.wait_429_cooldown)
    print(f"  mark_429{sig_mark}")
    print(f"  wait_429_cooldown{sig_wait}")

    params_mark = list(sig_mark.parameters.keys())
    params_wait = list(sig_wait.parameters.keys())
    assert "provider" in params_mark, f"mark_429: нет параметра provider (есть {params_mark})"
    assert "provider" in params_wait, f"wait_429_cooldown: нет параметра provider (есть {params_wait})"
    print("  OK: provider параметр присутствует в обеих функциях")
except AssertionError as exc:
    print(f"  FAIL: {exc}")
    sys.exit(1)

# 4. Сигнатуры _do_llm_request и _do_llm_stream_request
print()
print("[4] Сигнатуры _do_llm_request и _do_llm_stream_request...")
try:
    sig_req = inspect.signature(llm_analyzer._do_llm_request)
    sig_str = inspect.signature(llm_analyzer._do_llm_stream_request)
    print(f"  _do_llm_request{sig_req}")
    print(f"  _do_llm_stream_request{sig_str}")

    params_req = list(sig_req.parameters.keys())
    params_str = list(sig_str.parameters.keys())
    assert "provider" in params_req, f"_do_llm_request: нет provider (есть {params_req})"
    assert "provider" in params_str, f"_do_llm_stream_request: нет provider (есть {params_str})"
    print("  OK: provider параметр присутствует в обоих transport'ах")
except AssertionError as exc:
    print(f"  FAIL: {exc}")
    sys.exit(1)

# 5. _ask_llm_stream_free / _ask_llm_stream_paid передают provider
print()
print("[5] Проверка source кода _ask_llm_stream_free / _paid на provider=...")
try:
    src_free = inspect.getsource(llm_analyzer._ask_llm_stream_free)
    src_paid = inspect.getsource(llm_analyzer._ask_llm_stream_paid)
    assert 'provider="free"' in src_free, "_ask_llm_stream_free не передаёт provider='free'"
    assert 'provider="paid"' in src_paid, "_ask_llm_stream_paid не передаёт provider='paid'"
    print("  OK: _ask_llm_stream_free → provider='free'")
    print("  OK: _ask_llm_stream_paid → provider='paid'")
except AssertionError as exc:
    print(f"  FAIL: {exc}")
    sys.exit(1)

# 6. deepseek-v4-flash в pricing table
print()
print("[6] Pricing table — deepseek-v4-flash присутствует...")
try:
    from llm_analyzer import _LLM_PRICING_USD_PER_1M_TOKENS
    assert "deepseek-v4-flash" in _LLM_PRICING_USD_PER_1M_TOKENS, (
        "deepseek-v4-flash ОТСУТСТВУЕТ в pricing table — cost=N/A продолжит появляться"
    )
    pricing = _LLM_PRICING_USD_PER_1M_TOKENS["deepseek-v4-flash"]
    print(f"  deepseek-v4-flash: input=${pricing['input']}/1M, output=${pricing['output']}/1M")
    print("  OK: deepseek-v4-flash в pricing table")
except AssertionError as exc:
    print(f"  FAIL: {exc}")
    sys.exit(1)

# 7. Изоляция cooldown: mark_429(provider="free") не активирует cooldown для paid
print()
print("[7] Изоляция cooldown: 429 от free НЕ блокирует paid...")

async def test_isolation():
    # Сброс
    from llm_analyzer import _llm_429_until, _get_429_lock
    async with _get_429_lock():
        _llm_429_until["free"] = 0.0
        _llm_429_until["paid"] = 0.0

    # Симулируем 429 от free
    await llm_analyzer.mark_429(retry_after=None, provider="free")
    print("  mark_429(provider='free') — установлен cooldown для free")

    # ВНИМАНИЕ: НЕ вызываем wait_429_cooldown(provider="free") —
    # эта функция блокирует asyncio-loop на ~60 сек, пока cooldown
    # не истечёт. Читаем _llm_429_until напрямую.
    async with _get_429_lock():
        free_remaining_direct = _llm_429_until["free"] - time.monotonic()
        paid_remaining_direct = _llm_429_until["paid"] - time.monotonic()
    print(f"  free cooldown remaining: {free_remaining_direct:.1f} сек")
    print(f"  paid cooldown remaining: {paid_remaining_direct:.1f} сек "
          f"(<= 0 = не активен)")
    assert free_remaining_direct > 50, (
        f"free cooldown должен быть ~60 сек, но {free_remaining_direct}"
    )
    assert paid_remaining_direct <= 0, (
        f"paid cooldown НЕ должен быть активен после 429 от free, "
        f"но remaining={paid_remaining_direct}"
    )
    print("  OK: 429 от free НЕ активирует cooldown для paid — изоляция работает")

    # Сброс обратно, чтобы не мешать другим тестам
    async with _get_429_lock():
        _llm_429_until["free"] = 0.0
        _llm_429_until["paid"] = 0.0

try:
    asyncio.run(test_isolation())
except AssertionError as exc:
    print(f"  FAIL: {exc}")
    sys.exit(1)
except Exception as exc:
    print(f"  FAIL (unexpected exception): {exc}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

print()
print("=== SMOKE-TEST PASSED ===")
print()
print("Все 7 проверок прошли. Патч per-provider cooldown валиден.")
print()
print("Ожидаемый эффект после деплоя:")
print("  * 429 от free (GLM-4.7-Flash) НЕ блокирует платный (DeepSeek)")
print("  * Пользователь при 429 от free может сразу переключиться на paid")
print("    и получить ответ без 60-сек ожидания")
print("  * deepseek-v4-flash теперь в pricing table — cost будет логироваться")
print("    в логах LLM stream done")
print()
print("Деплой-инструкция:")
print("  1. Скопировать llm_analyzer.py на VPS:")
print("     cp llm_analyzer.py /path/to/MiniAPPgibdd/llm_analyzer.py")
print("  2. Рестарт приложения: supervisorctl restart miniapp")
print("  3. Проверить в логах:")
print("     - при 429 от free: 'LLM 429 cooldown activated for provider=free: 60s'")
print("     - при запросе к paid: 'LLM stream (paid): после 429-cooldown (0s) — продолжаем запрос'")
print("       (т.е. НЕ ждёт cooldown от free)")
print("     - после paid-генерации: 'cost=$X.XX' вместо 'cost=N/A (model not in pricing table)'")
