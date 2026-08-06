# GIBDD Stat Bot — Test Suite

Полный набор unit- и integration-тестов для проекта GIBDD-bot
(Telegram-бот + FastAPI mini-app для анализа ДТП).

**Текущее состояние:**

| Метрика                | Значение                                  |
|------------------------|-------------------------------------------|
| Всего тестов           | **359**                                   |
| Все тесты проходят     | ✅ (`359 passed in 4.94s`)                 |
| Общее покрытие кода    | **76.94 %** (порог в `pytest.ini` — 40 %) |
| Волн тестирования      | Wave 1 ✅ · Wave 2 ✅ · Wave 3 ✅          |
| Найдено и фиксировано багов | **3** (в `user_request_parser.py`)   |

---

## Содержимое архива

```
tests/
├── README.md                          ← этот файл
├── conftest.py                        ← общие фикстуры (Wave 1 + Wave 2)
├── __init__.py
│
├── fixtures/
│   ├── __init__.py
│   └── synthetic_cards.py             ← синтетические карточки ДТП
│
├── unit/                              ← Wave 1 + Wave 2: unit-тесты
│   ├── __init__.py
│   ├── test_analytics_metrics.py      ← 19 тестов · calculate_metrics
│   ├── test_analytics_compare.py      ← 10 тестов · compare_metrics
│   ├── test_analytics_cross_tables.py ← 8 тестов  · calculate_cross_tables
│   ├── test_analytics_stats.py        ← 33 теста  · point_statistics
│   ├── test_gibdd_parser.py           ← 37 тестов · parse_card_to_row
│   ├── test_gibdd_service.py          ← 25 тестов · gibdd_service (mock HTTP) [Wave 2]
│   ├── test_gibdd_service_cache.py    ← 8 тестов  · кэш in-memory
│   ├── test_llm_analyzer_format.py    ← 50 тестов · format_metrics_for_prompt [Wave 2]
│   ├── test_llm_analyzer_ask.py       ← 25 тестов · ask_paid_llm / ask_free_llm (mock) [Wave 2]
│   ├── test_telegram_auth.py          ← 18 тестов · Telegram initData HMAC [Wave 2]
│   └── test_user_request_parser.py    ← 42 теста  · parse_period / find_region / etc.
│
└── integration/                       ← Wave 2 + Wave 3: integration-тесты
    ├── __init__.py
    ├── _gibdd_stubs.py                ← shared stubs для Wave 3 (bot/parser/analytics/excel/LLM) [Wave 3]
    ├── test_routes.py                 ← 20 тестов · FastAPI routes через TestClient [Wave 2]
    ├── test_analyze_flow.py           ← 23 теста · execute_task полный pipeline + ensure_comparison + point_stats + LLM summary [Wave 3]
    ├── test_task_lifecycle.py         ← 6 тестов  · end-to-end lifecycle через FastAPI TestClient [Wave 3]
    ├── test_error_paths.py            ← 15 тестов · edge cases: таймауты, падения модулей, cleanup_old_tasks [Wave 3]
    └── test_clusters_flow.py          ← 20 тестов · start_clusters_calculation + Excel/HTML генерация + _serialize_cluster [Wave 3]
```

**Дополнительно нужно положить рядом с `tests/`:**

| Файл                 | Назначение                                                |
|----------------------|-----------------------------------------------------------|
| `pytest.ini`         | Конфигурация pytest, маркеры, порог покрытия              |
| `requirements-dev.txt` | Зависимости для тестирования (pytest, respx, freezegun) |

---

## Быстрый старт

### 1. Установка зависимостей

```bash
cd gibdd-bot
pip install -r requirements.txt        # основные зависимости проекта
pip install -r requirements-dev.txt    # зависимости для тестов
```

`requirements-dev.txt` ставит:

- `pytest >= 8.0`
- `pytest-asyncio >= 0.23` (режим `auto`)
- `pytest-cov >= 5.0`
- `respx >= 0.21` (mock для `httpx`)
- `freezegun >= 1.5` (mock для времени)
- `coverage >= 7.4`

### 2. Запуск всех тестов

```bash
pytest
```

Ожидаемый вывод:

```
295 passed in 3.87s

Required test coverage of 40% reached. Total coverage: 62.30%
Coverage HTML written to dir tests/_coverage_html
```

### 3. Запуск с деталями

```bash
pytest -v                              # подробный список тестов
pytest tests/unit/test_analytics_metrics.py   # один файл
pytest tests/unit/test_analytics_metrics.py::TestCalculateMetrics::test_total_accidents
                                       # один конкретный тест
```

### 4. HTML-отчёт покрытия

```bash
pytest                                 # запускает и генерит отчёт
# открыть в браузере:
xdg-open tests/_coverage_html/index.html
```

### 5. Фильтрация по маркерам

```bash
pytest -m "not slow"                   # без медленных тестов
pytest -m smoke                        # только smoke
pytest -m integration                  # только интеграционные
```

---

## Структура тестирования — 4 волны

Тесты спроектированы послойно, от чистых функций к интеграционным сценариям.
Каждая волна независима и может запускаться отдельно.

### Wave 1 — Чистые функции (✅ завершена)

Тестирует модули без внешних зависимостей:

- `analytics.py` → `calculate_metrics`, `compare_metrics`, `calculate_cross_tables`
- `point_statistics.py` → агрегаты по точкам
- `gibdd_parser.py` → `parse_card_to_row`
- `user_request_parser.py` → `parse_period`, `find_region`, `parse_user_message`
- Кэш in-memory в `gibdd_service.py` (через `id(cards)`)

**Фикстуры:** `tests/fixtures/synthetic_cards.py` — `BASE_CARD` + 7 готовых вариантов
(`card_with_death`, `card_with_alcohol`, `card_with_pedestrian`, и т.д.)

**Найдено багов: 3** (см. раздел «Исправленные баги» ниже)

### Wave 2 — Моки для LLM и сервисов (✅ завершена)

Тестирует модули с внешними HTTP-зависимостями, используя моки:

- `llm_analyzer.py` → `format_metrics_for_prompt` (50 тестов на форматирование)
  и `ask_paid_llm` / `ask_free_llm` (25 тестов с моками `httpx` через `respx`)
- `miniapp/backend/telegram_auth.py` → проверка HMAC-подписи Telegram initData
  (18 тестов, включая corrupted hash, replay, expired auth_date)
- `miniapp/backend/services/gibdd_service.py` → endpoint `/analyze` с моком
  внешнего API ГИБДД (25 тестов)
- `miniapp/backend/routers/*` → 20 интеграционных тестов через `TestClient`
  FastAPI с переопределённой зависимостью `get_current_user`

**Ключевые фикстуры `conftest.py` (Wave 2):**

| Фикстура                 | Что делает                                                |
|--------------------------|-----------------------------------------------------------|
| `patch_llm_keys`         | Подменяет `LLM_API_KEY`, `LLM_PAID_API_KEY` на тестовые   |
| `reset_llm_clients`      | Сбрасывает глобальные `_free_llm_client` / `_paid_llm_client` |
| `disable_rate_limiter`   | Отключает `_MIN_LLM_INTERVAL` (иначе тесты ждут по 5 сек) |
| `telegram_init_data_factory` | Генерирует валидный initData с правильной HMAC-подписью |
| `test_bot_token`         | Фиксирует `TELEGRAM_BOT_TOKEN` для тестов                 |
| `fastapi_test_user`      | Возвращает `TelegramUser` для override-авторизации        |
| `fastapi_client`         | FastAPI `TestClient` с уже подменённой авторизацией       |
| `clear_in_memory_tasks`  | Чистит `_tasks` в `gibdd_service` до/после теста          |
| `sample_comparison`      | Минимальный `comparison dict` для тестов форматирования   |

### Wave 3 — End-to-end integration (✅ завершена)

Тестирует **полный пайплайн** `execute_task` от создания задачи до готовых
файлов, а также все длительные операции (clusters, point_stats, LLM summary,
Excel/HTML генерация). Внешние модули (bot, gibdd_parser, analytics,
excel_generator, report_generator, llm_analyzer, point_statistics,
concentration_points, camera_cache, camera_matcher) подменяются stub'ами
через `_gibdd_stubs.py`.

**Ключевой файл:** `tests/integration/_gibdd_stubs.py` — фабрика stub-модулей
с конфигурируемыми параметрами (cards, prev_cards, errors, raise, llm_answer,
has_cameras, config_overrides).

| Тест-файл                           | Тестов | Что покрывает |
|-------------------------------------|--------|---------------|
| `test_analyze_flow.py`              | 23     | `execute_task` happy path, переходы статусов, error paths (empty cards, bot exception, task not found), `ensure_prev_cards` (4 кейса), `ensure_comparison` (4 кейса), `compute_point_stats` (2 кейса), `start_llm_summary` (3 кейса), `ask_llm_question` (3 кейса), `get_llm_providers_status` (2 кейса) |
| `test_task_lifecycle.py`            | 6      | End-to-end через FastAPI TestClient: create → poll → done → GET files, structured/text/failed modes, LLM summary polling, cached summary, QA history через эндпоинты |
| `test_error_paths.py`               | 15     | Edge cases: excel_generator crash, report_generator crash (карта опциональна), analytics fallback, multi-month prev loading, cleanup_old_tasks с файлами и без, LLM summary с invalid provider, LLM exception, cached comparison, ask_llm history preserved |
| `test_clusters_flow.py`             | 20     | `start_clusters_calculation` happy/failed/cameras, `_serialize_cluster` (4 кейса), `generate_clusters_map_html` (4 кейса), `generate_clusters_excel`, `generate_point_stats_excel`, `generate_point_stats_map_html`, `_color_for_severity` |

### Wave 4 — Golden / Smoke (⏳ планируется)

- `golden` — replay захваченных ответов LLM для регрессии формата
- `smoke` — быстрые проверки живости прод-эндпоинтов (раз в час в CI)

---

## Покрытие по модулям

| Модуль                                  | Stmts | Miss | Cover | 
|-----------------------------------------|-------|------|-------|
| `analytics.py`                          | 943   | 421  | 55 %  |
| `gibdd_parser.py`                       | 249   | 2    | **99 %** |
| `llm_analyzer.py`                       | 791   | 111  | 86 %  |
| `miniapp/backend/services/gibdd_service.py` | 934 | 177  | **81 %** ⬆ |
| `miniapp/backend/telegram_auth.py`      | 60    | 0    | **100 %** |
| `user_request_parser.py`                | 211   | 24   | 89 %  |
| **ИТОГО**                               | 3188  | 735  | **76.94 %** |

`gibdd_service.py` после Wave 3 вырос с 31 % до 81 % — все ключевые
функции (`execute_task`, `ensure_prev_cards`, `ensure_comparison`,
`compute_point_stats`, `start_clusters_calculation`, `start_llm_summary`,
`ask_llm_question`, `generate_clusters_*`, `generate_point_stats_*`,
`cleanup_old_tasks`) теперь покрыты.

`analytics.py` (55 %) — большая часть непокрытых строк это
`build_full_analytics` (сложная агрегирующая функция) и SQL-like
фильтры. Это цель Wave 4 golden-тестов (replay захваченных ответов ГИБДД).

---

## Исправленные баги (Wave 1)

Все 3 бага были в `user_request_parser.py` и найдены через property-based тесты.

### BUG #1: Регулярное выражение для кварталов

**Было:** `r"(?:(i{1,2}v?|vi{0,3}|iv|v|ix|x{1,3})\s*(?:кв|квартал))"`

**Проблема:** `i{1,2}v?` матчит только I, II, IV — но не III квартал.
III квартал вообще не распознавался, запросы вида «III квартал 2024»
возвращали `None`.

**Стало:** `i{1,3}v?` — теперь матчит I, II, III, IV.

**Тест:** `test_parse_period_quarters` проверяет все 4 квартала.

### BUG #2: Пустая строка в `find_region`

**Было:** функция сразу начинала нормализацию и пыталась искать вхождения.

**Проблема:** При `text_lower = ""` (пустой ввод) функция проходила по всем
регионам и для каждого проверяла `if word in normalized:` — это работало,
потому что пустая строка содержится в любой. В итоге `find_region("")`
возвращал **первый регион из справочника**, а не `None`.

**Стало:** добавлен ранний возврат:
```python
if not text_lower:
    return None
```

**Тест:** `test_find_region_empty_string_returns_none`.

### BUG #3: Substring-матч регионов

**Было:** `if word in normalized:` — простой `in` без границ слов.

**Проблема:** Запрос «москва» находил не только «г. Москва», но и любой
регион, где в названии есть подстрока «москва» (например, «Московская
область» — это другое). Также «орел» матчит «Орёл», «Орловская область»,
и любой регион с «орел» внутри названия — выбирался первый попавшийся.

**Стало:** `if re.search(r'\b' + re.escape(word) + r'\b', normalized):`
— матч только по границам слов.

**Тест:** `test_find_region_does_not_match_substring` — проверяет, что
«москва» не возвращает «Московская область».

---

## Конфигурация `pytest.ini`

```ini
[pytest]
asyncio_mode = auto                          # async-тесты запускаются без @pytest.mark.asyncio
testpaths = tests
strict_markers = true                        # неизвестный маркер = ошибка
addopts =
    -ra
    --strict-markers
    --cov=analytics
    --cov=user_request_parser
    --cov=gibdd_parser
    --cov=llm_analyzer
    --cov=backend.telegram_auth
    --cov=backend.services.gibdd_service
    --cov-report=term-missing
    --cov-report=html:tests/_coverage_html
    --cov-fail-under=40                      # CI упадёт, если покрытие < 40 %

markers =
    slow:        тесты дольше 1 секунды
    integration: требуют БД или внешние сервисы
    golden:      replay захваченных ответов LLM
    smoke:       быстрые проверки живости прод-эндпоинтов

filterwarnings =
    ignore::DeprecationWarning:pytest_asyncio.*
    ignore:coroutine '.*' was never awaited:RuntimeWarning
```

---

## Как добавить новый тест

### Unit-тест для чистой функции

1. Открой соответствующий файл в `tests/unit/test_<module>.py`
   (или создай новый, если модуль ещё не покрыт).
2. Используй `BASE_CARD` / `make_card(**overrides)` из
   `tests/fixtures/synthetic_cards.py` для данных.
3. Имя функции — `test_<что_проверяется>_<условие>`.
4. Не используй `time.sleep` — бери `freezegun.freeze_time`.

### Тест с моком HTTP (respx)

```python
import respx
import httpx

@respx.mock
async def test_my_endpoint():
    respx.post("https://api.example.com/v1").respond(
        json={"result": "ok"},
    )
    # ... вызов функции, которая делает httpx-запрос ...
```

### Тест FastAPI route

Используй фикстуру `fastapi_client` — она уже подменяет Telegram-авторизацию:

```python
def test_my_route(fastapi_client):
    response = fastapi_client.get("/api/v1/regions")
    assert response.status_code == 200
```

### Тест LLM-вызова

Используй `patch_llm_keys` + `reset_llm_clients` + `disable_rate_limiter`:

```python
@respx.mock
async def test_ask_paid_llm(patch_llm_keys, reset_llm_clients, disable_rate_limiter):
    respx.post("https://test.example.com/v1").respond(
        json={"choices": [{"message": {"content": "Анализ готов"}}]},
    )
    result = await llm_analyzer.ask_paid_llm("промпт")
    assert result == "Анализ готов"
```

### Тест gibdd_service pipeline (Wave 3)

Используй `install_stubs` из `_gibdd_stubs.py`:

```python
from tests.integration._gibdd_stubs import install_stubs, make_minimal_cards

@pytest.mark.asyncio
async def test_execute_task(monkeypatch, clear_in_memory_tasks, tmp_path):
    from backend.services import gibdd_service
    monkeypatch.setattr(gibdd_service, "_PROJECT_ROOT", tmp_path)

    install_stubs(monkeypatch, cards=make_minimal_cards(3))

    task = gibdd_service.create_task(
        user_id=1, region_code="1101", region_name="Рег",
        period_label="Май 2025", dat_list=["5.2025"], raw_query="q",
    )
    await gibdd_service.execute_task(task.id)

    assert task.status == gibdd_service.TaskStatus.DONE
    assert task.total_dtp == 3
```

Stub'ы конфигурируются: `cards`, `prev_cards`, `bot_errors`, `bot_raise`,
`llm_answer`, `has_cameras`, `config_overrides`, `record_bot_calls`.

---

## CI/CD интеграция

Минимальный GitHub Actions:

```yaml
name: tests
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - run: pip install -r requirements.txt -r requirements-dev.txt
      - run: pytest --cov-fail-under=40
```

`pytest.ini` уже настроен так, что упадёт, если:
- любой тест упадёт;
- покрытие упадёт ниже 40 %;
- будет использован незарегистрированный маркер.

---

## Что дальше

- **Wave 4** — golden-тесты (replay захваченных ответов LLM для регрессии
  формата) и smoke-тесты для прода. Цель: покрыть `analytics.build_full_analytics`
  (55 % → 75 %+), ловить регрессии в формате LLM-промптов при изменении
  метрик/кросс-таблиц.
- **Phase 3-2** — рефакторинг `bot.py` (4138 строк → модульная структура).
  Теперь можно запускать безопасно: 359 тестов покрывают все ключевые
  сценарии в `gibdd_service.py` и FastAPI routes. Любой регресс в
  `execute_task` / `ensure_comparison` / LLM / clusters будет пойман за ~5 сек.
