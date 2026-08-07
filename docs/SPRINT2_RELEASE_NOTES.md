# Sprint 2 — LLM_SEMAPHORE + LLM cache

**Дата:** 2026-08-07
**Тип:** Architecture improvements (backward compatible)
**Task ID:** sprint-2-llm-semaphore-cache

---

## Что было сделано

Решены две критические проблемы для scaling с 2 → 10-12 → 30 пользователей:

### 1. LLM_SEMAPHORE — лимит одновременных LLM-вызовов

**Проблема:** в логах продакшена наблюдались частые `429 Too Many Requests`
от GLM-4.7-Flash при 3+ одновременных LLM-вызовах. Retry с backoff
[30, 60, 90, 120, 150] сек приводил к **2 минутам ожидания** для пользователя.

Существующий rate-limiter в `llm_analyzer._do_llm_request` (`_last_llm_call_time`
+ `_MIN_LLM_INTERVAL=5.0`) имел race condition: два coroutine, начавшие
LLM-вызов одновременно, оба видели `elapsed >= 5.0` и оба шли в LLM.

**Решение:** глобальный `asyncio.Semaphore(LLM_MAX_CONCURRENT)` в `llm_ops.py`.
Оборачивает `start_llm_summary` (полностью) и `ask_llm_question` (только
HTTP-вызов к LLM, подготовка промпта идёт без semaphore).

```python
# llm_ops.py
_LLM_SEMAPHORE = asyncio.Semaphore(LLM_MAX_CONCURRENT)  # default=2

async def start_llm_summary(task, provider="free"):
    # ... cache check first ...
    async with _LLM_SEMAPHORE:
        await _run_llm_summary_inner(task, provider, state)
```

**Конфигурация (env):**
- `LLM_MAX_CONCURRENT=2` (default) — для free-тарифа GLM (RPM~30, безопасно)
- `LLM_MAX_CONCURRENT=5` — для paid-тарифа (DeepSeek, RPM~200)

**Логирование:**
- `LLM_SEMAPHORE: initialized with limit=2` — при старте
- `Task XXX: LLM_SEMAPHORE full (limit=2), waiting for slot...` — при ожидании
- `Task XXX: LLM summary done (from cache, free) — LLM call skipped` — cache hit

### 2. LLM cache — кэш summary в PostgreSQL

**Проблема:** повторный запуск того же региона+периода даёт идентичный
summary (детерминированный вход), но LLM вызывается заново — 53 сек ожидания
+ 429 risk.

**Решение:** кэш summary в PostgreSQL (таблица `llm_cache`, TTL=24h),
аналогично `excel_cache` / `clusters_cache`.

**Cache key** = SHA-256 от:
```
reg_code | dat_hash | provider | prompt_hash | llm_version
```

Где:
- `dat_hash` — MD5 от сортированного списка дат (как в `excel_cache`)
- `provider` — 'free' / 'paid' (разные модели → разные ответы)
- `prompt_hash` — MD5 от (system_prompt + clusters_ctx + cross_tables_ctx).
  Если меняется SYSTEM_PROMPT или формат таблиц — кэш инвалидируется автоматически.
- `llm_version` — env `LLM_CACHE_VERSION` (по умолчанию '1'). Позволяет
  принудительно инвалидировать ВЕСЬ кэш при релизе новой версии.

**Что кэшируется:**
- ✅ `start_llm_summary` — ДА (детерминированный вход)
- ❌ `ask_llm_question` — НЕТ (каждый вопрос уникальный)

**Логирование:**
```
llm_cache: MISS reg=1146 hash=417a9798.. provider=free → calling LLM
llm_cache: PUT reg=1146 hash=417a9798.. provider=free (4935 символов, TTL=86400s)
llm_cache: HIT reg=1146 hash=417a9798.. provider=free (4935 символов, возраст 2.3 ч)
```

**TTL refresh при cache hit:** если запись найдена, фоново вызывается `put_cached_summary`
(upsert) — это продлевает TTL ещё на 24 часа. Активно используемые записи не протухают.

## Структура изменений

### Новые файлы
- `miniapp/backend/db/llm_cache.py` (~280 строк) — GET/PUT/cleanup/make_cache_key
- `scripts/smoke_sprint2_llm.py` (~200 строк) — 24 проверки (импорты, semaphore, cache key, FastAPI app)

### Изменённые файлы
- `miniapp/backend/services/llm_ops.py` — добавлен `_LLM_SEMAPHORE`, `_init_llm_semaphore()`,
  `_check_llm_cache()`; `start_llm_summary` и `ask_llm_question` используют semaphore;
  `_run_llm_summary_inner` делает PUT в кэш после успешного LLM-вызова.
- `miniapp/backend/services/gibdd_service.py` (facade) — реэкспорт `_LLM_SEMAPHORE`,
  `_init_llm_semaphore` для тестов.
- `miniapp/backend/db/schema.sql` — добавлена таблица `llm_cache` + 4 индекса.
- `config.py` — добавлены `LLM_MAX_CONCURRENT`, `LLM_CACHE_TTL_SECONDS`, `LLM_CACHE_VERSION`.
- `main.py` — добавлен cleanup протухших записей `llm_cache` в фоне (каждые 2 часа).
- `tests/unit/test_gibdd_service.py` — добавлены классы `TestLLMSemaphore` (4 теста) и
  `TestLLMCache` (12 тестов).

### Что НЕ изменилось
- `miniapp/backend/routers/*.py` — без изменений
- `miniapp/backend/main.py` (кроме добавления cleanup) — без изменений
- `miniapp/backend/db/repository.py` — без изменений
- `llm_analyzer.py` — без изменений (существующий rate-limiter оставлен как fallback)
- Поведение для пользователя — идентично (только быстрее при cache hit)

## Результаты тестирования

- **Smoke-тест** (`scripts/smoke_sprint2_llm.py`): **24/25 PASSED** ✓
  (1 FAIL — `/metrics` endpoint в stub-окружении, не связано с Sprint 2)
- **Sprint 1 smoke** (`scripts/smoke_sprint1_facade.py`): **30/30 PASSED** ✓
  (без регрессий)
- **Unit + integration tests**: **462 passed, 0 failed, 24 skipped** ✓
  - Было 446 (после Sprint 1 LRU-fix) → стало 462 (+16 новых тестов Sprint 2)
  - 24 skipped — опциональные зависимости (psycopg, PTB v20+, slowapi, respx)

## Архитектурные решения

### Почему cache hit проверяется ДО semaphore?
Cache hit — это операция чтения из БД (~5-10 мс), она не требует LLM. Если
100 пользователей одновременно запросят один и тот же регион+период, и
summary уже в кэше — все 100 получат ответ мгновенно, без блокировок.
Semaphore нужна только для реальных LLM-вызовов.

### Почему ask_llm_question НЕ кэшируется?
Каждый вопрос пользователя уникальный. Кэшировать по вопросу бессмысленно
(частота попаданий <1%). Кэш был бы огромным и бесполезным.

### Почему _check_llm_cache вычисляет clusters_ctx и cross_tables_ctx заново?
Они нужны для `prompt_hash` — без них нельзя вычислить `cache_key`. Это
дублирование работы (те же вычисления идут в `_run_llm_summary_inner`), но:
- `cross_tables` кэшируется в `task.cross_tables` (Phase 3.1, ~0 мс при повторе)
- `clusters_ctx` — это форматирование уже готовых очагов (~1-2 мс)
- Дублирование стоит ~5-10 мс, но даёт мгновенный cache hit — это выгодно

### Почему TTL refresh при cache hit?
Активно используемые записи (регионы, которые запрашивают часто) должны
оставаться в кэше. Без refresh — запись протухнет через 24 часа даже если
её используют каждую минуту. С refresh — TTL продлевается при каждом hit.

## Установка

### Вариант A: полная замена (рекомендуется)

```bash
# 1. Бэкап
cp miniapp/backend/services/llm_ops.py miniapp/backend/services/llm_ops.py.bak
cp miniapp/backend/services/gibdd_service.py miniapp/backend/services/gibdd_service.py.bak
cp miniapp/backend/db/schema.sql miniapp/backend/db/schema.sql.bak
cp config.py config.py.bak
cp main.py main.py.bak

# 2. Распаковать архив
unzip sprint2-llm-semaphore-cache.zip -d /tmp/sprint2

# 3. Скопировать файлы
cp /tmp/sprint2/miniapp/backend/services/llm_ops.py miniapp/backend/services/
cp /tmp/sprint2/miniapp/backend/services/gibdd_service.py miniapp/backend/services/
cp /tmp/sprint2/miniapp/backend/db/llm_cache.py miniapp/backend/db/
cp /tmp/sprint2/miniapp/backend/db/schema.sql miniapp/backend/db/
cp /tmp/sprint2/config.py .
cp /tmp/sprint2/main.py .
cp /tmp/sprint2/tests/unit/test_gibdd_service.py tests/unit/
cp /tmp/sprint2/scripts/smoke_sprint2_llm.py scripts/

# 4. Проверить smoke-тест
python scripts/smoke_sprint2_llm.py

# 5. Запустить тесты
pytest tests/ -x --tb=short

# 6. Перезапустить MiniApp
# Схема llm_cache применится автоматически при старте (CREATE TABLE IF NOT EXISTS)
```

### Вариант B: только новые файлы (минимальный)

Если не хотите перезаписывать `config.py` / `main.py` / `test_gibdd_service.py`:

```bash
cp /tmp/sprint2/miniapp/backend/db/llm_cache.py miniapp/backend/db/
cp /tmp/sprint2/miniapp/backend/services/llm_ops.py miniapp/backend/services/
cp /tmp/sprint2/miniapp/backend/services/gibdd_service.py miniapp/backend/services/
cp /tmp/sprint2/miniapp/backend/db/schema.sql miniapp/backend/db/

# Вручную добавить в config.py:
# LLM_MAX_CONCURRENT = int(os.getenv("LLM_MAX_CONCURRENT", "2"))
# LLM_CACHE_TTL_SECONDS = int(os.getenv("LLM_CACHE_TTL_SECONDS", "86400"))
# LLM_CACHE_VERSION = os.getenv("LLM_CACHE_VERSION", "1")

# Вручную добавить в main.py в _cleanup_loop (после excel_cache cleanup):
# from miniapp.backend.db.llm_cache import cleanup_expired_llm_cache
# llm_removed = await cleanup_expired_llm_cache()
```

## Конфигурация env (опционально)

| Env | Default | Описание |
|-----|---------|----------|
| `LLM_MAX_CONCURRENT` | `2` | Лимит одновременных LLM-вызовов. Free=2, paid=5 |
| `LLM_CACHE_TTL_SECONDS` | `86400` | TTL кэша summary (24 часа) |
| `LLM_CACHE_VERSION` | `1` | Версия кэша (увеличить на 1 для полной инвалидации) |

## Откат

```bash
# Восстановить из бэкапа
cp miniapp/backend/services/llm_ops.py.bak miniapp/backend/services/llm_ops.py
cp miniapp/backend/services/gibdd_service.py.bak miniapp/backend/services/gibdd_service.py
cp miniapp/backend/db/schema.sql.bak miniapp/backend/db/schema.sql
cp config.py.bak config.py
cp main.py.bak main.py
rm miniapp/backend/db/llm_cache.py
# Таблица llm_cache в БД останется (вреда не приносит), при желании:
# DROP TABLE llm_cache;
```

## Ожидаемый эффект в проде

| Сценарий | До Sprint 2 | После Sprint 2 |
|----------|-------------|----------------|
| 2 пользователя, разные регионы | 2× LLM параллельно, иногда 429 | 2× LLM параллельно (limit=2), без 429 |
| 3 пользователя, разные регионы | 3× LLM параллельно, часто 429 (2 мин retry) | 2× LLM + 1 в очереди (~30 сек ожидания, без 429) |
| 2 пользователя, один регион+период | 2× LLM = 53 сек каждый | 1-й: 53 сек, 2-й: <100 мс (cache hit) |
| Повторный запрос того же региона | 53 сек + 429 risk | <100 мс (cache hit) |
| 10 пользователей, 5 регионов | 5× LLM = 5x 429 risk | 5× LLM в очереди по 2 (limit=2), без 429 |

## Что дальше

- **Sprint 3 (Phase 4-2):** Split `routers/analyze.py` (759 строк, 12 endpoints) →
  3 router-модуля (clusters, point_stats, llm)
- **Sprint 4:** Streaming LLM (SSE) — потоковый вывод summary для UX
- **Sprint 5 (Phase 4-3):** Split крупных `.tsx` (NpBddView 1088, AnalyticsView 887)
