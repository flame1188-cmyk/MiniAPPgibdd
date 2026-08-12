"""
core/fetching.py — синхронная выгрузка карточек ДТП (Sprint 7, Фаза C.2).

Единственная публичная функция:
- fetch_cards_for_period_sync(dat_list, reg_code, log_prefix, cache_result)
  → (cards, errors)

Назначение:
  Celery worker (Фаза C.3) — синхронный, не имеет event loop. Чтобы
  переиспользовать существующую async-функцию bot._fetch_cards_for_period
  (которая внутри делает httpx-запросы + web_fallback + кэш), мы вызываем
  её через asyncio.run() в отдельном event loop.

  Это работает, потому что:
  1. Celery worker (prefork model) запускает каждую задачу в отдельном
     subprocess — нет конкурирующего event loop.
  2. asyncio.run() создаёт новый loop, выполняет coroutine, закрывает loop.
  3. bot._fetch_cards_for_period не зависит от глобального event loop state
     (каждый httpx.AsyncClient создаётся внутри функции).

  Если в будущем потребуется prefetching (несколько параллельных выгрузок
  в одном worker-процессе), нужно будет переписать на sync httpx.Client.
  Сейчас это не нужно — Semaphore(3) в pipeline.execute_task уже ограничивает
  параллелизм, а Celery --concurrency=3 даст тот же эффект.

Возвращает:
  (cards, errors) — точно как bot._fetch_cards_for_period.
  cards: list[dict] — карточки ДТП (fields: id, dat, reg, pog, ran, ...).
  errors: list[str] — список строк-ошибок (может быть пустым).

Исключения:
  Любые исключения от bot._fetch_cards_for_period пробрасываются наверх.
  Celery-таск (C.3) должен их ловить и помечать задачу как FAILED.
"""
from __future__ import annotations

import asyncio
import logging
from typing import List, Tuple

# Импортируем _imports из services/ — он знает, как найти корень проекта
# и импортировать bot.* и другие модули gibdd-bot.
from ..services._imports import _import_module

logger = logging.getLogger(__name__)


def fetch_cards_for_period_sync(
    dat_list: List[str],
    reg_code: str,
    log_prefix: str = "Celery[fetch]",
    cache_result: bool = True,
) -> Tuple[List[dict], List[str]]:
    """Синхронно выгружает карточки ДТП за список месяцев.

    Sync-обёртка над bot._fetch_cards_for_period (async).
    Запускает async-функцию в отдельном event loop через asyncio.run().

    Args:
        dat_list: Список месяцев в формате "M.YYYY" (например ["1.2025", "2.2025"]).
        reg_code: Код региона (например "1101" — Республика Башкортостан).
        log_prefix: Префикс для логов.
        cache_result: Если True — сохраняет результат в cards_cache (PostgreSQL).

    Returns:
        Кортеж (cards, errors):
        - cards: список словарей с полями карточек ДТП
        - errors: список строк-ошибок (например ["Месяц 13.2025: API timeout"])

    Raises:
        RuntimeError: Если модуль bot не найден.
        Любые исключения от bot._fetch_cards_for_period (network, parsing, ...).

    Пример (Celery task в Фазе C.3):
        from miniapp.backend.core import fetch_cards_for_period_sync

        @app.task(queue="gibdd")
        def fetch_cards_task(dat_list, reg_code):
            cards, errors = fetch_cards_for_period_sync(
                dat_list=dat_list,
                reg_code=reg_code,
                log_prefix=f"Celery[task={current_task_id}]",
            )
            return {"cards_count": len(cards), "errors": errors}
    """
    bot_module = _import_module("bot")
    # bot.py реэкспортирует _fetch_cards_for_period из bot.access
    if not hasattr(bot_module, "_fetch_cards_for_period"):
        # Fallback: импортируем напрямую из bot.access
        try:
            bot_access = _import_module("bot.access")
            fetch_fn = bot_access._fetch_cards_for_period
        except (ImportError, AttributeError) as exc:
            raise RuntimeError(
                f"bot._fetch_cards_for_period не найден ни в bot, ни в bot.access: {exc}"
            ) from exc
    else:
        fetch_fn = bot_module._fetch_cards_for_period

    # Важно: asyncio.run() создаёт новый event loop, выполняет coroutine,
    # корректно закрывает loop. Если мы уже внутри event loop (например,
    # функция вызвана из FastAPI async-path) — это упадёт с RuntimeError.
    # Для FastAPI path есть async-обёртка в pipeline.execute_task.
    try:
        cards, errors = asyncio.run(
            fetch_fn(
                dat_list=dat_list,
                reg_code=reg_code,
                log_prefix=log_prefix,
                cache_result=cache_result,
            )
        )
    except RuntimeError as exc:
        if "asyncio.run() cannot be called from a running event loop" in str(exc):
            raise RuntimeError(
                "fetch_cards_for_period_sync() вызван из running event loop. "
                "Используйте bot._fetch_cards_for_period напрямую (await) "
                "или вызывайте из sync-контекста (Celery worker)."
            ) from exc
        raise

    logger.info(
        f"{log_prefix}: fetch_cards_for_period_sync вернул "
        f"{len(cards)} ДТП, {len(errors)} ошибок"
    )
    return cards, errors
