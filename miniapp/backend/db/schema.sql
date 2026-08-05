-- ============================================================
-- Схема БД для GIBDD Mini App (Этап 2: tasks + access_log)
-- ============================================================
-- Запуск: python -m miniapp.backend.db.init_schema
-- Все запросы идемпотентны (IF NOT EXISTS), можно пере-запускать.
-- ============================================================

-- ============================================================
-- tasks: метаданные задач выгрузки (персистентное хранилище
-- вместо in-memory _tasks: dict в gibdd_service.py).
--
-- Тяжёлые поля (cards, prev_cards, raw_clusters) НЕ хранятся в БД —
-- они остаются in-memory или пере-вычисляются при необходимости.
-- В БД хранится только то, что нужно для:
--   1. Отображения задачи в UI (статус, прогресс, totals, files)
--   2. Истории задач пользователя (list_user_tasks)
--   3. Аудита обращений к ПДн (152-ФЗ)
-- ============================================================
CREATE TABLE IF NOT EXISTS tasks (
    id              VARCHAR(32)   PRIMARY KEY,
    user_id         BIGINT        NOT NULL,
    region_code     VARCHAR(16)   NOT NULL,
    region_name     TEXT          NOT NULL,
    period_label    TEXT          NOT NULL,
    dat_list        JSONB         NOT NULL,    -- ["1.2026", "2.2026", ...]
    raw_query       TEXT,
    status          VARCHAR(32)   NOT NULL DEFAULT 'pending',
    progress        INT           NOT NULL DEFAULT 0,
    error           TEXT,
    total_dtp       INT           NOT NULL DEFAULT 0,
    total_dead      INT           NOT NULL DEFAULT 0,
    total_injured   INT           NOT NULL DEFAULT 0,
    files           JSONB         NOT NULL DEFAULT '[]'::jsonb,
    analytics       JSONB,                     -- результат analytics (опционально)
    clusters_result JSONB,                     -- результат clusters_state.result (опционально)
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

-- История задач пользователя (самые свежие наверху)
CREATE INDEX IF NOT EXISTS idx_tasks_user_created
    ON tasks(user_id, created_at DESC);

-- Для очистки старых задач по возрасту
CREATE INDEX IF NOT EXISTS idx_tasks_created_at
    ON tasks(created_at);

-- ============================================================
-- access_log: аудит обращений к ПДн (требование 152-ФЗ).
-- Каждая запись = одно действие пользователя:
--   - create_task: создал задачу выгрузки
--   - download_file: скачал Excel/HTML
--   - view_clusters: открыл вкладку «Очаги»
--   - view_point_stats: запросил статистику по точке
--   - llm_query: задал вопрос LLM по данным
-- ============================================================
CREATE TABLE IF NOT EXISTS access_log (
    id              BIGSERIAL     PRIMARY KEY,
    user_id         BIGINT        NOT NULL,
    region_code     VARCHAR(16),
    period_label    TEXT,
    action          VARCHAR(64)   NOT NULL,
    task_id         VARCHAR(32),
    details         JSONB,
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_access_log_user_id
    ON access_log(user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_access_log_created_at
    ON access_log(created_at DESC);

-- ============================================================
-- updated_at триггер для tasks (авто-обновление при UPDATE)
-- ============================================================
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_tasks_updated_at ON tasks;
CREATE TRIGGER trg_tasks_updated_at
    BEFORE UPDATE ON tasks
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();


-- ============================================================
-- dtp_cards_cache: кэш карточек ДТП в PostgreSQL (Этап 3).
-- Заменяет in-memory LRU из data_cache.py на персистентное хранилище,
-- разделяемое между всеми воркерами и переживающее рестарт.
--
-- Ключ кэша: (reg_code, dat_hash) где
--   dat_hash = MD5 от отсортированного списка "m.YYYY" дат,
--   склеенных через ','. Пример:
--     dat_list = ["1.2026", "2.2026"] → dat_hash = MD5("1.2026,2.2026")
--
-- Это даёт стабильный ключ, не зависящий от порядка месяцев в массиве
-- (сортируем перед хэшированием), и позволяет использовать в кэше
-- составные запросы за несколько периодов сразу.
--
-- TTL: expires_at = created_at + TTL_SECONDS (по умолчанию 1 час).
-- Записи с expires_at < NOW() считаются протухшими и игнорируются
-- при SELECT. Физическая очистка — через cleanup_old_cards() или
-- background job (см. db/cards_cache.py).
-- ============================================================
CREATE TABLE IF NOT EXISTS dtp_cards_cache (
    id              BIGSERIAL    PRIMARY KEY,
    reg_code        VARCHAR(16)  NOT NULL,
    dat_hash        CHAR(32)     NOT NULL,            -- MD5 hash
    dat_list        JSONB        NOT NULL,            -- ["1.2026","2.2026",...] для диагностики
    payload         JSONB        NOT NULL,            -- список карточек ДТП
    errors          JSONB        NOT NULL DEFAULT '[]'::jsonb,  -- ошибки выгрузки
    total_cards     INT          NOT NULL DEFAULT 0,
    source          VARCHAR(16)  NOT NULL DEFAULT 'api',  -- 'api' | 'web_fallback'
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ  NOT NULL
);

-- Уникальный индекс: одна запись на (reg_code, dat_hash).
-- На INSERT конфликтов (DO UPDATE) обновляем payload/expires_at.
CREATE UNIQUE INDEX IF NOT EXISTS uq_dtp_cards_cache_reg_dat
    ON dtp_cards_cache(reg_code, dat_hash);

-- Для самого частого запроса:
--   SELECT ... WHERE reg_code=%s AND dat_hash=%s AND expires_at > NOW()
-- ВАЖНО: НЕ используем partial index (WHERE expires_at > NOW()),
-- потому что NOW() — функция STABLE, а не IMMUTABLE. PostgreSQL
-- запрещает STABLE-функции в предикате partial index:
--   ERROR: functions in index predicate must be marked IMMUTABLE
-- Это рушит весь init_pool() и переводит приложение в in-memory fallback.
-- Обычный композитный индекс тоже эффективен: фильтр по expires_at > NOW()
-- применяется после индексного поиска по (reg_code, dat_hash) — для одной
-- записи это O(1).
CREATE INDEX IF NOT EXISTS idx_dtp_cards_cache_reg_dat_expires
    ON dtp_cards_cache(reg_code, dat_hash, expires_at);

-- Для cleanup_old_cards() — быстрый поиск протухших записей.
CREATE INDEX IF NOT EXISTS idx_dtp_cards_cache_expires
    ON dtp_cards_cache(expires_at);

-- Для invalidate_by_region — быстрое удаление всех записей региона.
CREATE INDEX IF NOT EXISTS idx_dtp_cards_cache_reg
    ON dtp_cards_cache(reg_code);
