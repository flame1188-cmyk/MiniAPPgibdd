-- ─────────────────────────────────────────────────────────────────────────
-- TRUNCATE архива ДТП для перезагрузки с новой схемой kart_id
-- ─────────────────────────────────────────────────────────────────────────
-- kart_id меняется с 9-значного region(2)+year(2)+seq(5) на 17-значный
-- region(2)+year(2)+month(2)+day(2)+empt_number(9).
-- Старые данные несовместимы с новой схемой — удаляем всё и перезагружаем.
--
-- ВНИМАНИЕ: это разрушительная операция. Все 50,636 карт будут удалены
-- вместе с ТС, участниками и журналом ETL.
--
-- Stabilization P0 #2 / A7: ОБЯЗАТЕЛЬНО сделайте свежий бэкап перед запуском!
-- Если бэкапа нет или он старше 24 часов — TRUNCATE АБОРТНЁТСЯ (см. ниже).
--
-- Безопасный запуск:
--   1. Сначала сделайте бэкап (если не сделан в последние 24 часа):
--      PYTHONPATH=/app python3 /app/scripts/backup_archive.py
--   2. Проверьте, что бэкап есть и не повреждён:
--      PYTHONPATH=/app python3 /app/scripts/backup_archive.py --list
--   3. Только после этого запускайте truncate:
--      psql "$DATABASE_URL" -f scripts/truncate_archive.sql
--   или:
--      python3 -c "
--      import os, psycopg2
--      psycopg2.connect(os.environ['DATABASE_URL']).cursor().execute(open('scripts/truncate_archive.sql').read())
--      "
-- ─────────────────────────────────────────────────────────────────────────

-- ─────────────────────────────────────────────────────────────────────────
-- Stabilization P0 #2 / A7: предохранитель — проверка свежего бэкапа
-- ─────────────────────────────────────────────────────────────────────────
-- Перед разрушительным TRUNCATE проверяем, что в /app/data/backups/
-- есть .dump-файл свежее 24 часов. Если нет — abort с понятным сообщением.
--
-- Это НЕ защита от дурака (truncate можно сделать через psql напрямую),
-- но она catches «я забыл сделать бэкап» при запуске через скрипт.
DO $$
DECLARE
    backup_dir text := '/app/data/backups';
    dir_exists boolean;
    filename text;
    f_mtime timestamp;
    newest_mtime timestamp := NULL;
    has_backup boolean := false;
    age_hours float8;
BEGIN
    -- Проверяем существование директории
    BEGIN
        -- pg_stat_file возвращает record(size, access, modification, change, creation, isdir)
        SELECT isdir INTO dir_exists FROM pg_stat_file(backup_dir);
    EXCEPTION WHEN OTHERS THEN
        -- Если pg_stat_file выбросил exception — файла/директории нет или нет доступа
        RAISE EXCEPTION 'TRUNCATE ABORTED: backup directory % does not exist or PG has no access. '
                        'Сначала сделайте бэкап: '
                        'PYTHONPATH=/app python3 /app/scripts/backup_archive.py',
            backup_dir;
    END;

    IF NOT dir_exists THEN
        RAISE EXCEPTION 'TRUNCATE ABORTED: backup path % exists but is not a directory. '
                        'Сначала сделайте бэкап: '
                        'PYTHONPATH=/app python3 /app/scripts/backup_archive.py',
            backup_dir;
    END IF;

    -- Идём по всем файлам в директории
    FOR filename IN SELECT * FROM pg_ls_dir(backup_dir) LOOP
        IF filename LIKE 'gibdd_%' || '.dump' THEN
            has_backup := true;
            BEGIN
                -- pg_stat_file возвращает колонку modification (timestamp)
                SELECT modification INTO f_mtime FROM pg_stat_file(backup_dir || '/' || filename);
                IF newest_mtime IS NULL OR f_mtime > newest_mtime THEN
                    newest_mtime := f_mtime;
                END IF;
            EXCEPTION WHEN OTHERS THEN
                -- Пропускаем файлы, которые не удалось прочитать
                NULL;
            END;
        END IF;
    END LOOP;

    IF NOT has_backup THEN
        RAISE EXCEPTION 'TRUNCATE ABORTED: в % нет .dump файлов. '
                        'Сначала сделайте бэкап: '
                        'PYTHONPATH=/app python3 /app/scripts/backup_archive.py',
            backup_dir;
    END IF;

    -- newest_mtime в UTC (timestamp without timezone от pg_stat_file)
    -- now() тоже timestamp without timezone в timezone сессии
    -- Сравниваем разницу в часах
    age_hours := extract(epoch from (now() - newest_mtime)) / 3600;

    IF age_hours > 24 THEN
        RAISE EXCEPTION 'TRUNCATE ABORTED: последний бэкап сделан % часов назад '
                        '(> 24 часов). Сначала сделайте свежий бэкап: '
                        'PYTHONPATH=/app python3 /app/scripts/backup_archive.py',
            round(age_hours, 1);
    END IF;

    RAISE NOTICE 'Pre-flight check OK: свежий бэкап найден (возраст % часов). Truncate продолжается.',
        round(age_hours, 1);
END $$;

BEGIN;

-- TRUNCATE с CASCADE: чистит и vehicles, и participants (FK ON DELETE CASCADE)
-- RESTART IDENTITY: сбрасывает BIGSERIAL в 1, чтобы новые id не были огромными
TRUNCATE
    gibdd_cards,
    gibdd_vehicles,
    gibdd_participants,
    gibdd_cards_collisions,
    etl_log
RESTART IDENTITY
CASCADE;

-- Готово. etl_log тоже чистим — иначе per-(reg,dat) статусы 'done'
-- не дадут перезагрузить эти периоды.
COMMIT;

-- ─────────────────────────────────────────────────────────────────────────
-- После TRUNCATE нужно:
-- 1. Применить обновлённую schema.sql (с новым столбцом empt_number и таблицей
--    gibdd_cards_collisions). Schema идемпотентна (CREATE TABLE IF NOT EXISTS),
--    но IF NOT EXISTS не добавит столбец, если таблица уже существует.
--    Поэтому добавляем ALTER TABLE для добавления empt_number, если его нет:
-- ─────────────────────────────────────────────────────────────────────────

-- Добавляем столбец empt_number, если его нет (для случая, когда gibdd_cards
-- уже была создана старой версией schema.sql)
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_name = 'gibdd_cards'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'gibdd_cards' AND column_name = 'empt_number'
    ) THEN
        ALTER TABLE gibdd_cards ADD COLUMN empt_number VARCHAR(32);
        CREATE INDEX IF NOT EXISTS idx_cards_empt_number ON gibdd_cards (empt_number);
        RAISE NOTICE 'Добавлен столбец gibdd_cards.empt_number + индекс';
    END IF;
END $$;

-- Проверка финального состояния
SELECT
    (SELECT COUNT(*) FROM gibdd_cards) AS cards_count,
    (SELECT COUNT(*) FROM gibdd_vehicles) AS vehicles_count,
    (SELECT COUNT(*) FROM gibdd_participants) AS participants_count,
    (SELECT COUNT(*) FROM gibdd_cards_collisions) AS collisions_count,
    (SELECT COUNT(*) FROM etl_log) AS etl_log_count;
