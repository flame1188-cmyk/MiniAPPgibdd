"""
Управление async-пулом соединений к PostgreSQL (psycopg 3).

Жизненный цикл:
    lifespan startup  →  init_pool()  (создаёт пул, проверяет подключение)
    запрос            →  get_pool().getconn() / .putconn()
    lifespan shutdown →  close_pool() (закрывает пул)

Если DATABASE_URL не задан — пул не создаётся, все вызовы
repository возвращаются на in-memory fallback (см. repository.py).

Если пул создаётся, но первое подключение не удалось —
устанавливается флаг _DB_READY=False, и repository также
ушёл в fallback, чтобы не ронять приложение при недоступности БД.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from psycopg import AsyncConnection, OperationalError
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from ..config import settings

logger = logging.getLogger(__name__)

# Глобальный пул (создаётся в init_pool, закрывается в close_pool)
_pool: Optional[AsyncConnectionPool] = None

# True если пул создан И первое тестовое подключение прошло успешно.
# Если False — repository работает в in-memory fallback режиме.
_DB_READY: bool = False

# Путь к schema.sql (рядом с этим файлом)
SCHEMA_SQL_PATH = Path(__file__).parent / "schema.sql"


async def _configure_connection(conn: AsyncConnection) -> None:
    """
    Применяется к каждому новому соединению в пуле.

    Включает TCP keepalive на стороне PostgreSQL-клиента (libpq через
    server-side параметры tcp_keepalives_*), чтобы соединения не "зависали"
    после простоя. На VPS с NAT/iptables conntrack типичный idle timeout
    60-300 сек — без keepalive соединения становятся BAD и первый
    запрос после простоя падает с "SSL error: unexpected eof while reading".

    Параметры:
      tcp_keepalives_idle=30     — начать слать keepalive после 30 сек простоя
      tcp_keepalives_interval=10 — интервал между keepalive пакетами (10 сек)
      tcp_keepalives_count=3     — количество неудачных keepalive до закрытия
    """
    try:
        await conn.execute(
            "SET tcp_keepalives_idle = 30; "
            "SET tcp_keepalives_interval = 10; "
            "SET tcp_keepalives_count = 3;"
        )
    except Exception as exc:
        # Не роняем пул, если параметры недоступны (например, старая PG)
        logger.debug(f"_configure_connection: tcp_keepalives set failed: {exc}")


def is_db_ready() -> bool:
    """True если БД готова к работе (пул создан, тестовое подключение OK)."""
    return _DB_READY


async def init_pool() -> bool:
    """
    Создаёт async-пул соединений и проверяет подключение.

    Returns:
        True если пул создан и тестовое подключение прошло успешно,
        False если DATABASE_URL не задан ИЛИ подключение не удалось
        (в этом случае приложение продолжит работу с in-memory fallback).
    """
    global _pool, _DB_READY

    if not settings.db_enabled:
        logger.info(
            "DATABASE_URL не задан — задачи хранятся in-memory "
            "(как и раньше). См. .env.example для подключения PostgreSQL."
        )
        return False

    try:
        # min_size/max_size: пул соединений к PostgreSQL.
        # По умолчанию min=2, max=15 (см. config.py).
        # 15 соединений достаточно для 10-15 одновременных пользователей:
        #   - long-poll эндпоинты держат соединение 25-60 сек
        #   - _persist() в execute_task делает 6 коротких запросов
        #   - hits/misses кэшей — кратковременные соединения
        # При росте до 30 пользователей — увеличьте DB_POOL_MAX до 30-40.
        # timeout: на shared-хостинге PG может тормозить — даём 30 сек.
        #
        # check: пул вызывает этот callback перед выдачей соединения
        # из getconn(). AsyncConnectionPool.check_connection делает
        # SELECT 1 — ловит BAD-соединения, которые умерли по idle TCP
        # timeout (NAT/iptables conntrack). Без этого первый запрос
        # после простоя падает с "SSL error: unexpected eof while reading".
        # Оверхед ~1-2 мс на запрос.
        #
        # max_idle / max_lifetime: пул сам ре-циклирует соединения раньше,
        # чем сервер/NAT их закроет. Дефолты psycopg 600/3600 — слишком
        # долго для VPS-деплоя с NAT idle timeout 60-300 сек.
        #
        # configure: включает TCP keepalive на стороне клиента.
        _pool = AsyncConnectionPool(
            conninfo=settings.database_url,
            min_size=settings.db_pool_min,
            max_size=settings.db_pool_max,
            timeout=settings.db_connect_timeout,
            # dict_row — чтобы fetchone() возвращал dict, удобнее для
            # сериализации в JSON.
            kwargs={"row_factory": dict_row},
            # Проверять соединения перед выдачей (SELECT 1). Полностью
            # убирает "SSL eof" ошибки, добавляет ~1-2 мс на запрос.
            check=AsyncConnectionPool.check_connection,
            # Ре-цикл соединений. На VPS с NAT idle timeout 60-300 сек,
            # поэтому 120 сек max_idle — безопасно. max_lifetime 600 сек
            # ре-циклит даже активные соединения (защита от накопления
            # плохих TCP-сессий).
            max_idle=settings.db_pool_max_idle,
            max_lifetime=settings.db_pool_max_lifetime,
            # Сколько ждать реконнект при total outage (1 мин).
            reconnect_timeout=settings.db_pool_reconnect_timeout,
            # TCP keepalive на каждом новом соединении
            configure=_configure_connection,
            # Не открывать соединения синхронно при создании —
            # пусть пул открывает их по требованию.
            open=False,
        )
        # Открываем пул и проверяем подключение
        await _pool.open(wait=True)

        # Тестовое подключение + запуск схемы
        async with _pool.connection() as conn:
            # Проверка доступности
            await conn.execute("SELECT 1")
            # Идемпотентное создание схемы
            schema_sql = SCHEMA_SQL_PATH.read_text(encoding="utf-8")
            await conn.execute(schema_sql)
            await conn.commit()

        _DB_READY = True
        logger.info(
            f"PostgreSQL пул готов: "
            f"min={settings.db_pool_min}, max={settings.db_pool_max}, "
            f"schema применена ({SCHEMA_SQL_PATH.name})"
        )
        return True

    except OperationalError as exc:
        logger.error(
            f"Не удалось подключиться к PostgreSQL: {exc}. "
            f"Приложение продолжит работу с in-memory fallback. "
            f"Проверьте DATABASE_URL в .env"
        )
        _DB_READY = False
        # Закрываем пул, если он частично создался
        if _pool is not None:
            try:
                await _pool.close()
            except Exception:
                pass
            _pool = None
        return False
    except Exception as exc:
        logger.exception(
            f"Неожиданная ошибка при инициализации PostgreSQL: {exc}. "
            f"In-memory fallback активирован."
        )
        _DB_READY = False
        if _pool is not None:
            try:
                await _pool.close()
            except Exception:
                pass
            _pool = None
        return False


async def close_pool() -> None:
    """Закрывает пул соединений (вызывается при shutdown)."""
    global _pool, _DB_READY

    if _pool is not None:
        try:
            await _pool.close()
            logger.info("PostgreSQL пул закрыт")
        except Exception as exc:
            logger.warning(f"Ошибка при закрытии пула PostgreSQL: {exc}")
        finally:
            _pool = None
            _DB_READY = False


def get_pool() -> Optional[AsyncConnectionPool]:
    """
    Возвращает пул соединений или None если БД не готова.

    Использование в repository:
        pool = get_pool()
        if pool is None:
            return _in_memory_fallback(...)
        async with pool.connection() as conn:
            ...
    """
    if not _DB_READY or _pool is None:
        return None
    return _pool


async def health_check() -> dict:
    """
    Health-check для эндпоинта /health/db.
    Возвращает статус пула и метрики.
    """
    if not settings.db_enabled:
        return {
            "configured": False,
            "ready": False,
            "reason": "DATABASE_URL not set",
        }

    if not _DB_READY or _pool is None:
        return {
            "configured": True,
            "ready": False,
            "reason": "pool not initialized or connection failed",
        }

    try:
        async with _pool.connection() as conn:
            cur = await conn.execute("SELECT 1 AS ok")
            row = await cur.fetchone()
            stats = _pool.get_stats()
            return {
                "configured": True,
                "ready": True,
                "test_query": row,
                "pool_stats": stats,
            }
    except Exception as exc:
        return {
            "configured": True,
            "ready": False,
            "reason": f"health-check failed: {exc}",
        }
