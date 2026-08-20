#!/usr/bin/env python3
"""
scripts/monitor_archive.py — мониторинг размера архива PostgreSQL (P0 #2 / A7).

Проверяет:
  1. Размер таблиц архива (gibdd_cards, gibdd_vehicles, gibdd_participants, etl_log)
  2. Размер таблиц с кэшами (dtp_cards_cache, clusters_cache, excel_cache, llm_cache)
  3. Размер БД целиком (pg_database_size)
  4. Свободное место на диске /app/data

Алертит (logger.warning + exit code 1) если:
  - Размер архива > ARCHIVE_SIZE_LIMIT_GB (по умолчанию 10 ГБ, 80% от 15 ГБ bothost)
  - Размер БД > DB_SIZE_LIMIT_GB (по умолчанию 12 ГБ)
  - Свободное место на /app/data < DISK_FREE_LIMIT_GB (по умолчанию 3 ГБ)
  - Нет свежего бэкапа (старше ARCHIVE_BACKUP_MAX_AGE_HOURS, по умолчанию 30 часов)

Cron:
    0 6 * * *  PYTHONPATH=/app python3 /app/scripts/monitor_archive.py >> /app/data/backups/monitor.log 2>&1

Выход:
  0 — всё OK
  1 — есть предупреждения (алерты)
  2 — критическая ошибка (не смог подключиться к БД)
"""
from __future__ import annotations

import logging
import os
import sys
import asyncio
from datetime import datetime, timezone
from pathlib import Path

# Загрузка .env (как в backup_archive.py)
try:
    env_path = Path("/app/.env")
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] monitor_archive: %(message)s",
)
logger = logging.getLogger("monitor_archive")


# Конфигурация лимитов (можно переопределить через env)
ARCHIVE_SIZE_LIMIT_GB = float(os.environ.get("ARCHIVE_SIZE_LIMIT_GB", "10"))
DB_SIZE_LIMIT_GB = float(os.environ.get("DB_SIZE_LIMIT_GB", "12"))
DISK_FREE_LIMIT_GB = float(os.environ.get("DISK_FREE_LIMIT_GB", "3"))
ARCHIVE_BACKUP_MAX_AGE_HOURS = int(os.environ.get("ARCHIVE_BACKUP_MAX_AGE_HOURS", "30"))
BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", "/app/data/backups"))

# Таблицы архива ГИБДД
ARCHIVE_TABLES = [
    "gibdd_cards",
    "gibdd_vehicles",
    "gibdd_participants",
    "gibdd_cards_collisions",
    "etl_log",
    "gibdd_regions",
    "gibdd_indicators",
]

# Кэш-таблицы (менее важные, можно truncate при нехватке места)
CACHE_TABLES = [
    "dtp_cards_cache",
    "clusters_cache",
    "excel_cache",
    "llm_cache",
    "llm_sessions",
]


async def get_archive_sizes() -> dict[str, float]:
    """Возвращает размеры таблиц архива в МБ."""
    try:
        from miniapp.backend.db.connection import get_pool, is_db_ready
    except ImportError:
        # Пробуем прямой импорт для запуска из /app
        sys.path.insert(0, "/app/miniapp/backend")
        sys.path.insert(0, "/app")
        from db.connection import get_pool, is_db_ready  # type: ignore

    if not is_db_ready():
        logger.error("БД не готова (is_db_ready=False). Мониторинг невозможен.")
        sys.exit(2)

    pool = get_pool()
    if pool is None:
        logger.error("Пул соединений = None. Мониторинг невозможен.")
        sys.exit(2)

    sizes: dict[str, float] = {}
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            # Размер каждой таблицы через pg_total_relation_size (включая индексы + TOAST)
            all_tables = ARCHIVE_TABLES + CACHE_TABLES
            for table in all_tables:
                try:
                    await cur.execute(
                        "SELECT pg_total_relation_size(%s)",
                        (table,),
                    )
                    row = await cur.fetchone()
                    if row:
                        sizes[table] = (row[0] if isinstance(row, dict) else row["pg_total_relation_size"]) / (1024 * 1024)
                except Exception as exc:
                    logger.warning(f"Не удалось получить размер {table}: {exc}")
                    sizes[table] = 0.0

            # Размер БД целиком
            try:
                await cur.execute("SELECT pg_database_size(current_database())")
                row = await cur.fetchone()
                if row:
                    val = row[0] if isinstance(row, dict) else row["pg_database_size"]
                    sizes["__database_total__"] = val / (1024 * 1024)
            except Exception as exc:
                logger.warning(f"pg_database_size failed: {exc}")

            # Количество записей в gibbd_cards
            try:
                await cur.execute("SELECT COUNT(*) FROM gibdd_cards")
                row = await cur.fetchone()
                if row:
                    sizes["__gibdd_cards_count__"] = float(row[0] if isinstance(row, dict) else row["count"])
            except Exception as exc:
                logger.warning(f"COUNT gibdd_cards failed: {exc}")

    return sizes


def get_disk_free_gb(path: Path) -> float:
    """Возвращает свободное место на диске в ГБ."""
    try:
        stat = os.statvfs(str(path))
        return (stat.f_bavail * stat.f_frsize) / (1024 ** 3)
    except Exception as exc:
        logger.warning(f"statvfs failed for {path}: {exc}")
        return -1.0


def get_latest_backup_age_hours() -> float | None:
    """Возвращает возраст последнего бэкапа в часах, или None если бэкапов нет."""
    if not BACKUP_DIR.exists():
        return None

    backup_files = list(BACKUP_DIR.glob("gibdd_*.dump"))
    if not backup_files:
        return None

    latest_mtime = max(f.stat().st_mtime for f in backup_files)
    age_seconds = datetime.now(timezone.utc).timestamp() - latest_mtime
    return age_seconds / 3600


async def main() -> int:
    logger.info(f"=== Archive monitoring started at {datetime.now(timezone.utc).isoformat()} ===")

    alerts: list[str] = []

    # 1. Размеры БД и таблиц
    try:
        sizes = await get_archive_sizes()
    except Exception as exc:
        logger.error(f"Не удалось получить размеры из БД: {exc}")
        return 2

    archive_total_mb = sum(sizes.get(t, 0) for t in ARCHIVE_TABLES)
    cache_total_mb = sum(sizes.get(t, 0) for t in CACHE_TABLES)
    db_total_mb = sizes.get("__database_total__", 0)
    cards_count = int(sizes.get("__gibdd_cards_count__", 0))

    logger.info(f"Архив (gibdd_*):   {archive_total_mb:.1f} MB ({archive_total_mb/1024:.2f} GB)")
    logger.info(f"Кэш (dtp/clusters): {cache_total_mb:.1f} MB ({cache_total_mb/1024:.2f} GB)")
    logger.info(f"БД всего:           {db_total_mb:.1f} MB ({db_total_mb/1024:.2f} GB)")
    logger.info(f"Записей gibdd_cards: {cards_count:,}")

    if archive_total_mb / 1024 > ARCHIVE_SIZE_LIMIT_GB:
        alerts.append(
            f"Размер архива {archive_total_mb/1024:.2f} GB > лимита {ARCHIVE_SIZE_LIMIT_GB} GB"
        )

    if db_total_mb / 1024 > DB_SIZE_LIMIT_GB:
        alerts.append(
            f"Размер БД {db_total_mb/1024:.2f} GB > лимита {DB_SIZE_LIMIT_GB} GB"
        )

    # 2. Свободное место на диске
    # Проверяем /app (бэкапы) и / (если PG на другом VPS — то не критично, но проверим)
    for check_path in [Path("/app/data"), Path("/")]:
        free_gb = get_disk_free_gb(check_path)
        if free_gb < 0:
            continue
        logger.info(f"Свободно на {check_path}: {free_gb:.2f} GB")
        if free_gb < DISK_FREE_LIMIT_GB:
            alerts.append(
                f"Свободно на {check_path} только {free_gb:.2f} GB < лимита {DISK_FREE_LIMIT_GB} GB"
            )

    # 3. Свежесть бэкапа
    backup_age = get_latest_backup_age_hours()
    if backup_age is None:
        alerts.append(
            f"Бэкапов нет в {BACKUP_DIR}. Запустите backup_archive.py немедленно!"
        )
        logger.warning("Бэкапов нет!")
    else:
        logger.info(f"Последний бэкап сделан {backup_age:.1f} часов назад")
        if backup_age > ARCHIVE_BACKUP_MAX_AGE_HOURS:
            alerts.append(
                f"Последний бэкап старше {ARCHIVE_BACKUP_MAX_AGE_HOURS} часов "
                f"(фактически {backup_age:.1f} ч). Запустите backup_archive.py!"
            )

    # 4. Итог
    if alerts:
        logger.warning("=" * 60)
        logger.warning("⚠️ АЛЕРТЫ:")
        for a in alerts:
            logger.warning(f"  - {a}")
        logger.warning("=" * 60)
        return 1

    logger.info("Все проверки прошли без алертов.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
