"""
Управление async-пулом соединений к gibdd_db (ПАП данные).

Отдельный сервер PostgreSQL — отдельный пул с минимальными настройками.
Запросы лёгкие (агрегация по точкам), нужен маленький пул.

Жизненный цикл:
    lifespan startup  → init_pap_pool()
    запрос            →  get_pap_pool().getconn() / .putconn()
    lifespan shutdown → close_pap_pool()
"""
from __future__ import annotations

import logging
from typing import Optional

from psycopg import OperationalError
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from ..config import settings

logger = logging.getLogger(__name__)

_pool: Optional[AsyncConnectionPool] = None
_PAP_DB_READY: bool = False


async def _configure_pap_connection(conn) -> None:
    """TCP keepalive для соединений к gibdd_db."""
    try:
        old_autocommit = conn.autocommit
        conn.autocommit = True
        try:
            await conn.execute(
                "SET tcp_keepalives_idle = 30; "
                "SET tcp_keepalives_interval = 10; "
                "SET tcp_keepalives_count = 3;"
            )
        finally:
            conn.autocommit = old_autocommit
    except Exception as exc:
        logger.debug(f"PAP DB: tcp_keepalives set failed: {exc}")


def is_pap_db_ready() -> bool:
    """True если ПАП БД готова к работе."""
    return _PAP_DB_READY


def get_pap_pool() -> Optional[AsyncConnectionPool]:
    """Возвращает пул ПАП БД или None."""
    if not _PAP_DB_READY or _pool is None:
        return None
    return _pool


async def init_pap_pool() -> bool:
    """Создаёт пул к gibdd_db. Возвращает True если успешно."""
    global _pool, _PAP_DB_READY

    if not settings.pap_db_enabled:
        logger.info("PAP_DB_HOST не задан — слой ПАП на карте будет недоступен")
        return False

    try:
        _pool = AsyncConnectionPool(
            conninfo=settings.pap_db_url,
            min_size=settings.pap_db_pool_min,
            max_size=settings.pap_db_pool_max,
            timeout=15,
            kwargs={"row_factory": dict_row},
            check=AsyncConnectionPool.check_connection,
            max_idle=120,
            max_lifetime=600,
            reconnect_timeout=30,
            configure=_configure_pap_connection,
            open=False,
        )
        await _pool.open(wait=True)

        async with _pool.connection() as conn:
            await conn.execute("SELECT 1")

        _PAP_DB_READY = True
        logger.info(
            f"PAP DB (gibdd_db) пул готов: "
            f"host={settings.pap_db_host}, "
            f"min={settings.pap_db_pool_min}, max={settings.pap_db_pool_max}"
        )
        return True

    except OperationalError as exc:
        logger.warning(
            f"PAP DB: не удалось подключиться к gibdd_db: {exc}. "
            f"Слой ПАП будет недоступен."
        )
        _PAP_DB_READY = False
        if _pool is not None:
            try:
                await _pool.close()
            except Exception:
                pass
            _pool = None
        return False
    except Exception as exc:
        logger.warning(f"PAP DB: ошибка инициализации: {exc}")
        _PAP_DB_READY = False
        if _pool is not None:
            try:
                await _pool.close()
            except Exception:
                pass
            _pool = None
        return False


async def close_pap_pool() -> None:
    """Закрывает пул ПАП БД."""
    global _pool, _PAP_DB_READY
    if _pool is not None:
        try:
            await _pool.close()
            logger.info("PAP DB пул закрыт")
        except Exception as exc:
            logger.warning(f"Ошибка при закрытии пула ПАП БД: {exc}")
        finally:
            _pool = None
            _PAP_DB_READY = False
