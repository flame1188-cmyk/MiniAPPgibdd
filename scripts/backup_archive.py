#!/usr/bin/env python3
"""
scripts/backup_archive.py — резервное копирование архива ГИБДД (P0 #2 / A7).

Сохраняет в /app/data/backups/ дамп таблиц gibdd_* и etl_log через pg_dump.
По умолчанию используется сжатие (-Fc, custom format), что даёт 4-8x сжатие.

Retention: хранит последние N дней бэкапов (по умолчанию 5).
Старые файлы автоматически удаляются.

Cron:
    0 3 * * *  PYTHONPATH=/app python3 /app/scripts/backup_archive.py >> /app/data/backups/backup.log 2>&1

Или через supervisor:
    Добавьте как [program:backup] в supervisord.conf с_schedule через celery beat.

Запуск вручную:
    PYTHONPATH=/app python3 /app/scripts/backup_archive.py
    PYTHONPATH=/app python3 /app/scripts/backup_archive.py --full  # полный dump (все таблицы)

Exit codes:
    0 — успех
    1 — общая ошибка (DATABASE_URL не задан, нет pg_dump, дамп пустой, ...)
    2 — критическая ошибка во время дампа (возможно частичный бэкап оставлен для разбора)
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Загрузка .env если есть
try:
    from pathlib import Path as _Path
    env_path = _Path("/app/.env")
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
    format="%(asctime)s [%(levelname)s] backup_archive: %(message)s",
)
logger = logging.getLogger("backup_archive")

# Таблицы архива ГИБДД (для --partial режима по умолчанию).
# Если БД используется только для архива — этого достаточно.
# tasks/access_log/cache — отдельные таблицы, можно не бэкапить
# (они пересоздаются из БД при рестарте).
ARCHIVE_TABLES = [
    "gibdd_cards",
    "gibdd_vehicles",
    "gibdd_participants",
    "gibdd_cards_collisions",
    "etl_log",
    "gibdd_regions",
    "gibdd_indicators",
]

DEFAULT_BACKUP_DIR = Path("/app/data/backups")
DEFAULT_RETENTION_DAYS = 5


def get_database_url() -> str:
    """Получает DATABASE_URL из окружения."""
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        logger.error("DATABASE_URL не задан в окружении. Бэкап невозможен.")
        sys.exit(1)
    return url


def check_pg_dump() -> str:
    """Проверяет, что pg_dump доступен. Возвращает путь к бинарнику."""
    pg_dump = shutil.which("pg_dump")
    if not pg_dump:
        logger.error(
            "pg_dump не найден в PATH. Установите postgresql-client: "
            "apt-get install -y postgresql-client"
        )
        sys.exit(1)
    return pg_dump


def backup_dir() -> Path:
    """Возвращает директорию для бэкапов, создаёт если нужно."""
    backup_dir = Path(os.environ.get("BACKUP_DIR", str(DEFAULT_BACKUP_DIR)))
    backup_dir.mkdir(parents=True, exist_ok=True)
    return backup_dir


def make_backup(full: bool = False) -> Path:
    """Делает один бэкап через pg_dump -Fc (custom compressed format).

    Args:
        full: если True — все таблицы в БД. Иначе — только ARCHIVE_TABLES.

    Returns:
        Путь к созданному .dump файлу.
    """
    db_url = get_database_url()
    pg_dump = check_pg_dump()
    backup_d = backup_dir()

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    suffix = "full" if full else "archive"
    filename = f"gibdd_{suffix}_{timestamp}.dump"
    dump_path = backup_d / filename

    cmd = [pg_dump, "-Fc", "--no-owner", "--no-privileges"]
    if not full:
        for table in ARCHIVE_TABLES:
            cmd.extend(["-t", table])
    cmd.append(db_url)
    cmd.extend(["-f", str(dump_path)])

    logger.info(f"Запуск pg_dump: {filename}")
    logger.debug(f"Command: {' '.join(cmd)}")

    start = datetime.now(timezone.utc)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=1800,  # 30 min max
        )
    except subprocess.TimeoutExpired:
        logger.error("pg_dump timeout (30 min). Бэкап прерван.")
        if dump_path.exists():
            try:
                dump_path.unlink()
            except Exception:
                pass
        sys.exit(2)

    elapsed = (datetime.now(timezone.utc) - start).total_seconds()

    if result.returncode != 0:
        logger.error(f"pg_dump failed (rc={result.returncode}):")
        logger.error(f"stderr: {result.stderr[:2000]}")
        if dump_path.exists():
            try:
                dump_path.unlink()
            except Exception:
                pass
        sys.exit(2)

    size_mb = dump_path.stat().st_size / (1024 * 1024)
    logger.info(
        f"Бэкап готов: {filename} | "
        f"size={size_mb:.1f} MB | elapsed={elapsed:.1f}s"
    )

    # Проверка: дамп не должен быть пустым (< 1KB = что-то не так)
    if dump_path.stat().st_size < 1024:
        logger.error(
            f"Бэкап подозрительно мал (< 1KB): {dump_path.stat().st_size} байт. "
            f"Возможно, таблицы не существуют или пустые."
        )
        try:
            dump_path.unlink()
        except Exception:
            pass
        sys.exit(1)

    # Записываем метаданные рядом
    meta_path = dump_path.with_suffix(".meta")
    meta_path.write_text(
        f"filename={filename}\n"
        f"created_at={datetime.now(timezone.utc).isoformat()}\n"
        f"size_bytes={dump_path.stat().st_size}\n"
        f"mode={'full' if full else 'archive'}\n"
        f"tables={','.join(ARCHIVE_TABLES) if not full else 'ALL'}\n"
        f"pg_dump_version={subprocess.run([pg_dump, '--version'], capture_output=True, text=True).stdout.strip()}\n",
        encoding="utf-8",
    )

    return dump_path


def apply_retention(keep_days: int) -> int:
    """Удаляет бэкапы старше keep_days дней.

    Returns: количество удалённых файлов.
    """
    backup_d = backup_dir()
    if keep_days <= 0:
        return 0

    now = datetime.now(timezone.utc).timestamp()
    deleted = 0

    for entry in backup_d.iterdir():
        if not entry.is_file():
            continue
        if not entry.name.endswith(".dump"):
            continue

        mtime = entry.stat().st_mtime
        age_days = (now - mtime) / 86400
        if age_days > keep_days:
            try:
                entry.unlink()
                logger.info(f"Retention: удалён {entry.name} (возраст {age_days:.1f} дн)")
                # Также удаляем .meta файл если есть
                meta = entry.with_suffix(".meta")
                if meta.exists():
                    meta.unlink()
                deleted += 1
            except Exception as exc:
                logger.warning(f"Retention: не удалось удалить {entry.name}: {exc}")

    return deleted


def list_backups() -> list[dict]:
    """Возвращает список существующих бэкапов для отчёта."""
    backup_d = backup_dir()
    backups = []
    for entry in sorted(backup_d.iterdir()):
        if entry.is_file() and entry.name.endswith(".dump"):
            stat = entry.stat()
            backups.append({
                "filename": entry.name,
                "size_mb": stat.st_size / (1024 * 1024),
                "created_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            })
    return backups


def main():
    parser = argparse.ArgumentParser(
        description="Резервное копирование архива ГИБДД"
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Бэкап всех таблиц в БД (по умолчанию только gibdd_* + etl_log)",
    )
    parser.add_argument(
        "--retention",
        type=int,
        default=int(os.environ.get("BACKUP_RETENTION_DAYS", DEFAULT_RETENTION_DAYS)),
        help=f"Сколько дней хранить бэкапы (по умолчанию {DEFAULT_RETENTION_DAYS})",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Только показать существующие бэкапы и выйти",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Показать, что будет сделано, но не выполнять",
    )
    args = parser.parse_args()

    if args.list:
        backups = list_backups()
        if not backups:
            print("Бэкапов нет.")
        else:
            print(f"Найдено {len(backups)} бэкапов в {backup_dir()}:")
            for b in backups:
                print(f"  {b['filename']}  {b['size_mb']:.1f} MB  {b['created_at']}")
        return 0

    logger.info(
        f"=== Backup started | mode={'full' if args.full else 'archive'} | "
        f"retention={args.retention} дней | dry_run={args.dry_run} ==="
    )

    if args.dry_run:
        logger.info(f"DRY-RUN: будет создан бэкап в {backup_dir()}")
        logger.info(f"DRY-RUN: будут удалены бэкапы старше {args.retention} дней")
        backups = list_backups()
        logger.info(f"DRY-RUN: текущие бэкапы: {len(backups)} шт.")
        return 0

    # Шаг 1: сделать бэкап
    dump_path = make_backup(full=args.full)

    # Шаг 2: применить retention
    deleted = apply_retention(args.retention)
    if deleted > 0:
        logger.info(f"Retention: удалено {deleted} старых бэкапов")

    # Шаг 3: отчёт
    backups = list_backups()
    total_mb = sum(b["size_mb"] for b in backups)
    logger.info(
        f"=== Backup done | total_backups={len(backups)} | "
        f"total_size={total_mb:.1f} MB ==="
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
