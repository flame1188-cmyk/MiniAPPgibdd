# Резервное копирование и мониторинг архива ГИБДД (P0 #2 / A7)

## Что это

Два Python-скрипта + предохранитель в `truncate_archive.sql` для защиты от потери архива ГИБДД:

1. **`scripts/backup_archive.py`** — делает `pg_dump` архива (gibdd_* + etl_log) в `/app/data/backups/`, сжатый (-Fc, custom format), с retention (по умолчанию 5 дней)
2. **`scripts/monitor_archive.py`** — проверяет размер архива, БД, свободное место на диске, и свежесть последнего бэкапа; алертит при превышении лимитов
3. **`scripts/truncate_archive.sql`** (модифицированный) — перед `TRUNCATE` проверяет, что есть свежий бэкап (моложе 24 часов), иначе abort с понятной ошибкой

## Установка pg_dump

`pg_dump` не входит в Docker-образ по умолчанию. Проверьте:

```bash
docker exec <container> which pg_dump
```

Если пусто — добавьте в `Dockerfile`:

```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*
```

Или, если вы на bothost без Docker — `apt-get install -y postgresql-client` на самом сервере.

## Настройка cron

### Daily backup (главное)

```bash
# Открываем crontab
crontab -e

# Добавляем строку — каждый день в 03:00 UTC
0 3 * * *  PYTHONPATH=/app /usr/bin/python3 /app/scripts/backup_archive.py >> /app/data/backups/backup.log 2>&1

# Либо, если в Docker — через supervisor или через cron-демон в entrypoint.sh
```

### Monitoring

```bash
# Каждый день в 06:00 UTC — после backup
0 6 * * *  PYTHONPATH=/app /usr/bin/python3 /app/scripts/monitor_archive.py >> /app/data/backups/monitor.log 2>&1
```

### Weekly full backup (опционально, для надёжности)

```bash
# По воскресеньям в 04:00 UTC — полный бэкап всех таблиц
0 4 * * 0  PYTHONPATH=/app /usr/bin/python3 /app/scripts/backup_archive.py --full --retention 30 >> /app/data/backups/backup.log 2>&1
```

## Запуск вручную

```bash
# Сделать свежий бэкап (только архивные таблицы)
PYTHONPATH=/app python3 /app/scripts/backup_archive.py

# Сделать полный бэкап (все таблицы в БД, включая tasks/cache/access_log)
PYTHONPATH=/app python3 /app/scripts/backup_archive.py --full

# Посмотреть существующие бэкапы
PYTHONPATH=/app python3 /app/scripts/backup_archive.py --list

# Dry-run — показать, что будет сделано
PYTHONPATH=/app python3 /app/scripts/backup_archive.py --dry-run

# Проверить размеры и свежесть бэкапа
PYTHONPATH=/app python3 /app/scripts/monitor_archive.py
```

## Восстановление из бэкапа

```bash
# Создаём новую БД (или TRUNCATE существующей)
PYTHONPATH=/app python3 /app/scripts/backup_archive.py --list
# Выбираем нужный .dump файл

# Восстанавливаем через pg_restore
pg_restore --dbname="$DATABASE_URL" --no-owner --no-privileges --clean --if-exists /app/data/backups/gibdd_archive_YYYYMMDD_HHMMSS.dump
```

## Параметры (env variables)

| Переменная | По умолчанию | Описание |
|---|---|---|
| `DATABASE_URL` | (обязательно) | Connection string PostgreSQL |
| `BACKUP_DIR` | `/app/data/backups` | Куда складывать .dump файлы |
| `BACKUP_RETENTION_DAYS` | `5` | Сколько дней хранить бэкапы |
| `ARCHIVE_SIZE_LIMIT_GB` | `10` | Лимит размера архива (для monitor) |
| `DB_SIZE_LIMIT_GB` | `12` | Лимит размера всей БД (для monitor) |
| `DISK_FREE_LIMIT_GB` | `3` | Минимум свободного места на диске (для monitor) |
| `ARCHIVE_BACKUP_MAX_AGE_HOURS` | `30` | Максимум возраста последнего бэкапа (для monitor) |

## Проверка предохранителя в truncate

После установки backup-скрипта и первого запуска, попробуйте запустить truncate — должно сработать:

```bash
psql "$DATABASE_URL" -f scripts/truncate_archive.sql
# NOTICE: Pre-flight check OK: свежий бэкап найден (возраст 0.5 часов). Truncate продолжается.
# ...
```

А если бэкапа нет или он старше 24 часов:

```bash
psql "$DATABASE_URL" -f scripts/truncate_archive.sql
# ERROR: TRUNCATE ABORTED: последний бэкап сделан 48.0 часов назад (> 24 часов)...
```

## Размеры

Архив ГИБДД:
- ~750 МБ/мес × 12 мес × 6 лет = ~5.4 ГБ сырых данных
- Сжатый dump (-Fc): ~1.5-2.0 ГБ (4x сжатие)
- 5 дней × 2 ГБ = ~10 ГБ max (помещается в 15 ГБ bothost)

Если архив вырастет до 10+ ГБ — нужна либо ротация бэкапов в облако (S3), либо переезд на VPS 30+ ГБ.

## Что делать, если backup_archive.py падает

1. **`pg_dump not found`** — установите `postgresql-client` (см. выше)
2. **`DATABASE_URL не задан`** — проверьте `.env`, `BACKUP_DIR` должен быть writable
3. **`timeout (30 min)`** — архив слишком большой, увеличьте timeout в скрипте (или перенесите backup на VPS с PostgreSQL)
4. **`< 1KB`** — таблицы не существуют или пустые, проверьте `psql "$DATABASE_URL" -c "SELECT COUNT(*) FROM gibdd_cards"`

## Что делать, если monitor_archive.py алертит

1. **Размер архива > лимита** — пора делать full backup + truncate старых месяцев или переезжать на VPS
2. **Размер БД > лимита** — `VACUUM FULL` на самых больших таблицах (это потребует место = размер таблицы), либо `TRUNCATE` старых месяцев с бэкапом
3. **Свободно на диске < лимита** — проверить бэкапы retention, удалить старые, либо `docker system prune -a`
4. **Бэкап старше 30 часов** — проверить cron, либо запустить вручную `python3 scripts/backup_archive.py`
