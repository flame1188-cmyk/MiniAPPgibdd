"""
Кэш загруженных данных ДТП — фасад над PostgreSQL (dtp_cards_cache) + архивом.

In-memory L1 кэш УДАЛЁН: сырые карточки дублируют данные из БД (gibdd_cards / dtp_cards_cache),
запрос к которым отрабатывает за миллисекунды по индексам. Хранение дубликатов в RAM:
  - Тратит до 150 МБ оперативной памяти без пользы
  - Создаёт проблему сталенности (in-memory переживает обновления БД)
  - Усложняет инвалидацию (нужно чистить оба уровня)

Текущая архитектура кэша карточек:
  1. dtp_cards_cache (PostgreSQL, TTL 7 дней) — для свежих API-данных
  2. gibdd_cards (архив, без TTL) — постоянные исторические данные
  3. GIBDD API / web_fallback — живой запрос при кэш-миссе

In-memory кэш сохранён только для:
  - Задач (task_registry._tasks + _TASKS_HEAVY_STATE)
  - OSM полигонов (concentration_points._memory_cache)

Используется:
  - _fetch_cards_for_period() в bot/access.py
  - preload-загрузкой в фоне после выгрузки текущего периода
  - miniapp/backend/services через bot._state
"""

import logging

logger = logging.getLogger(__name__)


# ============================================================
# Статистика кэша (для обратной совместимости с логированием)
# ============================================================
# После удаления in-memory L1 статистика всегда пустая.
# Сохраняем интерфейс, чтобы старые строки логов не ломались.

class _DummyStats:
    """Заглушка статистики кэша (in-memory L1 удалён)."""
    def stats(self) -> str:
        return "cache: L1 removed (DB-only)"

    def stats_dict(self) -> dict:
        return {"entries": 0, "valid": 0, "total_cards_cached": 0,
                "total_bytes_mb": 0, "max_bytes_mb": 0}

    def get(self, *args, **kwargs):
        return None

    def put(self, *args, **kwargs):
        pass

    def has(self, *args, **kwargs):
        return False

    def invalidate(self, *args, **kwargs):
        pass

    def invalidate_by_region(self, *args, **kwargs) -> int:
        return 0

    def clear(self):
        pass


# Глобальный экземпляр-заглушка для обратной совместимости
data_cache = _DummyStats()


# ============================================================
# Async-обёртки для PostgreSQL + Archive
# ============================================================
async def get_async(
    reg_code: str,
    dat_list: list[str],
    force_refresh: bool = False,
) -> tuple[list[dict], list[str]] | None:
    """
    Читает карточки из кэша (PostgreSQL dtp_cards_cache).

    При force_refresh=True — всегда возвращает None (пропускает кэш),
    чтобы данные были загружены заново из архива/API.

    Возвращает (cards, errors) или None.
    """
    if force_refresh:
        return None

    try:
        from miniapp.backend.db.cards_cache import get_cached_cards
        return await get_cached_cards(reg_code, dat_list)
    except Exception as e:
        logger.debug(f"data_cache.get_async: DB lookup failed: {e}")
        return None


async def put_async(
    reg_code: str,
    dat_list: list[str],
    cards: list[dict],
    errors: list[str],
    source: str = "api",
) -> None:
    """
    Сохраняет карточки в PostgreSQL dtp_cards_cache.
    """
    try:
        from miniapp.backend.db.cards_cache import put_cached_cards
        await put_cached_cards(reg_code, dat_list, cards, errors, source=source)
    except Exception as e:
        logger.debug(f"data_cache.put_async: DB write failed: {e}")


async def invalidate_by_region_async(reg_code: str) -> int:
    """
    Удаляет все записи кэша для региона из PostgreSQL.

    Возвращает количество удалённых записей.
    """
    try:
        from miniapp.backend.db.cards_cache import invalidate_region
        return await invalidate_region(reg_code)
    except Exception as e:
        logger.debug(f"data_cache.invalidate_by_region_async: DB failed: {e}")
        return 0


async def has_async(
    reg_code: str,
    dat_list: list[str],
    force_refresh: bool = False,
) -> bool:
    """Проверка наличия валидной записи в кэше."""
    return await get_async(reg_code, dat_list, force_refresh=force_refresh) is not None
