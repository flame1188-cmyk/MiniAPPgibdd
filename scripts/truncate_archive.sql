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
-- Запуск:
--   psql "$DATABASE_URL" -f scripts/truncate_archive.sql
-- или:
--   python3 -c "
--   import os, psycopg2
--   psycopg2.connect(os.environ['DATABASE_URL']).cursor().execute(open('scripts/truncate_archive.sql').read())
--   "
-- ─────────────────────────────────────────────────────────────────────────

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
