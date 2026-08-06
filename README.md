# GIBDD Stat — Telegram-бот + Mini App

Telegram-бот и веб-Mini App для выгрузки и анализа данных ДТП из открытых данных ГИБДД ([stat.gibdd.ru](http://stat.gibdd.ru)).

Проект объединяет:
- **Telegram-бот** (`bot.py`) — команды, inline-кнопки, выгрузка файлов в чат, нативные HTML-карты в attachment.
- **Mini App** (`miniapp/`) — FastAPI backend + React/TypeScript frontend, открывается в нативном WebView Telegram (решает проблему iOS Quick Look, не выполняющего JS).
- **Единая точка входа** (`main.py`) — поднимает FastAPI и Telegram-бота (webhook) в одном процессе, раздаёт Mini App на `/app/`.

Бот запрашивает данные через Open Data API или (при недоступности API) напрямую с сайта, парсит карточки ДТП и возвращает стилизованные Excel-файлы и интерактивные HTML-карты. Поддерживает естественный язык ввода, аналитику с сравнением периодов, расчёт очагов концентрации ДТП, сопоставление с камерами фотовидеофиксации, прогноз по НП БДД и AI-анализ данных через ZhipuAI GLM.

---

## Возможности

### Основной функционал

- **3 способа ввода запроса:** inline-кнопки (`/dtp`), естественный язык («Вологодская область за 2025 год»), строгий формат (`2.2024 1119`)
- **Два Excel-файла:** карточки ДТП (1 строка = 1 ДТП) и участники ДТП (1 строка = 1 участник)
- **Аналитика:** сравнение текущего периода с АППГ, распределение по дням недели, часам, видам ДТП, 25 кросс-таблиц корреляций, статистические метрики (severity rates, Z-score, χ²)
- **Очаги концентрации ДТП:** автоматический расчёт мест концентрации аварийности с новой методологией v2 (пикетаж + соседи + слияния)
- **Камеры фотовидеофиксации:** загрузка реестра камер через Excel-файл, кэширование по регионам, автоматическое сопоставление с очагами ДТП (по пикетажу и геопозиции)
- **Статистика по точке:** отправка геолокации для получения сводки ДТП в заданном радиусе
- **AI-анализ (ZhipuAI GLM-4.7-Flash):** генерация аналитического резюме, ответы на вопросы по данным, поиск новостей из открытых источников (Google News RSS + DuckDuckGo) для контекста
- **НП БДД (Национальный проект «Безопасные качественные дороги»):** история погибших, прогноз с сезонными коэффициентами, коридор прогноза, KPI-статус (ok/warning/danger), frozen-годы
- **Web-fallback:** при ошибке API (5xx, ConnectionError) автоматически переключается на экспорт через сайт stat.gibdd.ru (POST генерация + GET скачивание XML)
- **4-уровневый fallback справочника регионов:** API → файловый кэш → встроенный хардкод (82 региона) → пустой список
- **Трёхуровневый кэш данных ДТП:** L1 in-memory LRU → L2 PostgreSQL (cards/clusters/excel) → L3 файловый кэш. Экономия до 18-28 сек на повторных запросах того же региона+периода
- **Персистентность задач в PostgreSQL:** задачи выгрузки, кластеры и аудит-лог доступа к ПДн (152-ФЗ) хранятся в БД, переживают рестарт
- **Ограничение доступа:** optional whitelist по Telegram user ID

### Очаги концентрации ДТП (методология v2)

Полная переработка методологии сопоставления очагов между периодами. Старая методология (центр очага + радиус 500м/2км + совпадение дороги) заменена на более точную:

**Алгоритм сопоставления:**
- **Повторный очаг:** та же дорога + пересечение диапазонов пикетажа (`dtp_pk_min/max`) ИЛИ ДТП в радиусе 100м (для безпикетажных, типично НП)
- **Подстатус для повторного:** `growing` / `shrinking` / `stable` / `merged` (по изменению кол-ва ДТП)
- **Слияние:** 2+ прошлогодних очага пересекаются с одним текущим → `repeated_merged`
- **Новый (есть ближайший в АППГ):** не пересеклись, но в радиусе 1000м (вне НП) / 250м (в НП) есть прошлый очаг. Сохраняется список до 3 ближайших
- **Новый:** нет ни повтора, ни соседа
- **Исчезнувший:** прошлый, у которого нет повторного в текущем (сосед не спасает от lost)

**Расчёт очагов:**
1. **Населённые пункты** — 3 прохода: перекрёстки (радиус 50 м), дороги с пикетажем (окно 200 м), общие точки (радиус 100 м + проверка пикетажа). Порог: 3+ ДТП одного вида или 5+ любых видов
2. **Вне НП (автодороги)** — группировка по названию дороги, скользящее окно 1 км. Порог: 3+ ДТП одного вида или 5+ любых видов
3. **Определение границ НП/вне НП** через OSM Overpass API с реальными полигонами (Shapely). Оптимизации: in-memory LRU-кэш полигонов, адаптивный bbox, параллельные запросы к зеркалам, дисковый кэш с TTL, STRtree для классификации (вместо unary_union)

**Excel-отчёт по очагам (4 листа):**
- Очаги текущего периода
- Динамика (с исчезнувшими, повторными, новыми, новыми с соседом)
- Детализация всех ДТП в очагах
- Предочаги (потенциальные очаги следующего периода)

### Интерактивные HTML-карты

Бот и Mini App генерируют самодостаточные HTML-файлы с картами на базе Leaflet. Все библиотеки встраиваются инлайн — файлы работают без интернет-соединения (кроме подгрузки тайлов OpenStreetMap).

**Типы карт:**
- **Карта ДТП** — все ДТП региона с попапами, фильтрами (вид, тяжесть, дата) и опциональным слоем камер
- **Карта очагов** — очаги и предочаги ДТП с зонами (convex hull), динамикой (7 цветов по статусу), камерами, пунктирными линиями связи для «новых с соседом»
- **Карта точки** — ДТП и камеры в радиусе от заданной геоточки

**Возможности карт:**
- **Popup с расширенной информацией** — дата, адрес, тяжести, транспортные средства, погодные условия, дорожные условия, объекты УДС, нарушения ПДД и сопутствующие нарушения
- **Кластеризация маркеров** (Leaflet.markercluster) — для карт ДТП и точки кластеризуются и ДТП, и камеры; для карты очагов только камеры (ДТП немного)
- **Spiderfy** — при максимальном зуме наложенные маркеры «раскрываются» в спираль/линию для доступа к каждой точке
- **maxZoom: 19** — детальное приближение до уровня отдельных зданий
- **Линейка** — собственная реализация без внешних зависимостей: кнопка-переключатель 📏 в углу карты, клик добавляет точки отрезка, двойной клик завершает измерение
- **Фильтры на карте ДТП** — по виду ДТП, тяжести, диапазону дат, моделям камер (множественный выбор)
- **Управление слоями** — возможность скрывать/показывать слои ДТП и камер

### Mini App

Telegram Mini App, открывающийся в нативном WebView Telegram. Решает главную проблему iOS-пользователей — HTML-карты открываются в WebView, выполняющем JavaScript, а не в Quick Look.

**Вкладки Mini App:**
1. **Запрос** — структурированная форма (регион + период) или текстовый ввод
2. **Аналитика** — 25 кросс-таблиц, статистические метрики, ECharts-визуализации
3. **Очаги** — расчёт очагов концентрации, карта (iframe), KPI-сводка, динамика vs АППГ, Top-10 по тяжести, предочаги, Excel-выгрузка
4. **Точка** — статистика ДТП в радиусе от геоточки + карта
5. **ИИ-анализ** — генерация аналитического резюме (15-90 сек), Q&A с историей
6. **НП БДД** — история, прогноз, коридор, KPI-статус, управление frozen-годами

**Технические особенности:**
- **Long polling** (25 сек) для статуса длительных операций (очаги, LLM-резюме) — устраняет 30+ коротких запросов
- **Локальный флаг `starting`** для мгновенного показа прогресс-бара после клика, не дожидаясь первого long-poll ответа
- **Elasped-time тикер** для LLM-анализа с предупреждениями при > 90 сек и рекомендацией отмены при > 240 сек
- **Сброс кэша react-query** при retry после ошибки — иначе кнопка «Повторить» возвращает старую ошибку
- **Telegram theme** — автоматическое применение цветовой схемы (light/dark) через CSS-переменные
- **Haptic feedback** — нативная вибрация на клики/ошибки/успехи
- **Fullscreen mode** — кнопка раскрытия на десктопе

### Совместимость с мобильными устройствами (iOS)

На iPhone HTML-файлы из Telegram по умолчанию открываются в Quick Look, который не выполняет JavaScript — в результате карта отображается пустой. Для полноценной работы:

- **Рекомендуется:** открыть Mini App через кнопку-меню бота (нативный WebView, JS выполняется полностью)
- **Альтернатива 1:** установить приложение [HTML Viewer](https://apps.apple.com/app/html-viewer) (бесплатно) и открывать файлы через него
- **Альтернатива 2:** в Telegram долгое нажатие на файл → «Поделиться» → «Сохранить в Файлы» → открыть в приложении «Файлы» (файл откроется в Safari)

---

## Архитектура

### Единый процесс на bothost

```
                    ┌─────────────────────────────────┐
                    │   bothost.ru (TLS-терминация)    │
                    │   bot1234.bothost.tech           │
                    └────────────┬────────────────────┘
                                 │  HTTPS
                    ┌────────────▼────────────────────┐
                    │   main.py (FastAPI + uvicorn)    │
                    │   один процесс, PORT=$PORT       │
                    ├─────────────────────────────────┤
                    │  /bot/webhook  → Telegram bot    │
                    │  /api/*        → Mini App API    │
                    │  /app/*        → React static    │
                    │  /health       → healthcheck     │
                    └─────────────────────────────────┘
```

**Единый процесс**: `main.py` поднимает FastAPI и в lifespan инициализирует Telegram-бота в webhook-режиме. Mini App frontend (собранный React) раздаётся как статика из `miniapp/frontend/dist`.

### Канал данных: API → Fallback → Кэш

```
Запрос пользователя
       │
       ▼
┌──────────────┐    5xx / ConnectionError
│  GIBDD API   │──────────────────────┐
│ (opendataapi)│                      │
└──────┬───────┘                      ▼
       │                    ┌──────────────────┐
       │                    │  Сайт stat.gibdd │
       │                    │  POST → GET → XML│
       │                    └────────┬─────────┘
       │                             │
       ▼                             ▼
┌─────────────────────────────────────────┐
│         Единый формат карточек ДТП       │
│   (совместимый для API и web-fallback)  │
└─────────────────┬───────────────────────┘
                  │
        ┌─────────┼─────────┐
        ▼         ▼         ▼
   dtp_cards  dtp_uch   analytics
     .xlsx      .xlsx     .xlsx
```

### Трёхуровневый кэш данных ДТП (L1 + L2 + L3)

После загрузки и обработки данные кэшируются на трёх уровнях. На повторных запросах того же региона+периода генерация полностью пропускается:

```
Запрос (reg_code, dat_hash)
       │
       ▼
┌─────────────────────────────────────────────────────┐
│ L1: In-memory LRU (data_cache.py, 100 записей)       │
│    cards + prev_cards in-process, мгновенный HIT     │
└─────────────┬───────────────────────────────────────┘
              │ miss
              ▼
┌─────────────────────────────────────────────────────┐
│ L2: PostgreSQL (модуль miniapp/backend/db/)          │
│    • cards_cache    — JSONB с карточками ДТП          │
│    • clusters_cache — JSONB с raw_clusters + metrics  │
│    • excel_cache    — BYTEA с готовыми xlsx-файлами   │
│    TTL=86400s (24ч), фоновая очистка каждые 2 часа    │
└─────────────┬───────────────────────────────────────┘
              │ miss
              ▼
┌─────────────────────────────────────────────────────┐
│ L3: Файловый кэш (regions_cache, cameras, osm_cache)  │
└─────────────┬───────────────────────────────────────┘
              │ miss
              ▼
        Полная выгрузка из API/сайта ГИБДД
```

**Ключи кэша:**
- `cards_cache`, `excel_cache`: `(reg_code, dat_hash)` — хэш списка dat (месяцев)
- `clusters_cache`: `(reg_code, current_dat_hash, prev_dat_hash)` — зависит от пары периодов

**Экономия на повторных запросах** (подтверждено в проде):

| Stage | Что закэшировано | Экономия на HIT |
|-------|------------------|-----------------|
| Stage 3 | Карточки ДТП (cards_cache) | ~3-5 сек |
| Stage 4 | Кластеры + raw_clusters (clusters_cache) | ~8-15 сек (DBSCAN) |
| Stage 5 | Excel Файл 1 + Файл 2 (excel_cache) | ~7-8 сек |

Совокупная экономия: **~18-28 сек** на повторном запросе. Кэш особенно эффективен, когда несколько сотрудников ГИБДД выгружают один регион за тот же период.

### Загрузка справочника регионов

```
1. Модульный кэш (_regions_cache в памяти)
   ↓ (не загружен)
2. API ГИБДД (fetch_regions) [можно отключить через REGIONS_API_ENABLED=0]
   ↓ (успех → сохранение в data/regions_cache.json)
   ↓ (ошибка: 5xx, таймаут)
3. Файловый кэш (data/regions_cache.json)
   ↓ (файл отсутствует)
4. Встроенный справочник (regions_builtin.py, 82 региона)
```

### Сопоставление с камерами

`camera_matcher.py` реализует три стратегии:
1. Линейный очаг с пикетажем → камера на том же участке дороги (по пикетажу)
2. Линейный очаг без камеры на участке → гео-поиск от крайних точек (1 км НП / 500 м вне НП)
3. Очаг без пикетажа (НП) → гео-поиск 200 м (закрытый) и 500 м (ближайшие)

### LLM-контекст для AI-анализа

Промпт для GLM формируется из нескольких секций:

```
┌─────────────────────────────────────────┐
│  Системный промпт (роль аналитика ГИБДД) │
└─────────────────┬───────────────────────┘
                  │
   ┌──────────────┼──────────────┐
   ▼              ▼              ▼
 Comparison   Clusters      Cross-tables
 (метрики     (разделены     (25 таблиц
  АППГ vs      на категории:  корреляций +
  текущий)     повторные/     статистика:
               новые/         severity rates,
               исчезнувшие)   Z-score, χ²)
                  │
                  ▼
         Q&A history (последние 6 пар)
```

**Разделение очагов по категориям для LLM** (метод `format_clusters_for_prompt`):
- **ПОВТОРНЫЕ** — текущие очаги, которые были и в АППГ. Показываем динамику (было X → стало Y)
- **НОВЫЕ** — текущие очаги, которых не было в АППГ. Для `new_with_neighbor` указываем ближайший АППГ-очаг
- **ИСЧЕЗНУВШИЕ** — очаги прошлого периода, которых больше нет

В Top-10 по тяжести для UI передаются только текущие очаги (исключая `is_lost` и `is_prev_matched`).

---

## Быстрый старт

### 1. Клонирование и установка

```bash
git clone https://github.com/<your-username>/gibdd-stat-bot.git
cd gibdd-stat-bot

# Создайте виртуальное окружение
python -m venv venv

# Активируйте его
# Linux/Mac:
source venv/bin/activate
# Windows:
venv\Scripts\activate

# Установите зависимости
pip install -r requirements.txt
```

Либо на Windows:
```bash
install.bat
```

### 2. Настройка

```bash
cp .env.example .env
```

Заполните в `.env`:

| Переменная | Описание | Обязательно |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Токен бота от [@BotFather](https://t.me/BotFather) | Да |
| `BOTHOST_DOMAIN` | Домен bothost (для webhook) | Для production |
| `PORT` | Порт FastAPI (bothost передаёт через `$PORT`) | Для production |
| `CORS_ORIGINS` | Origins для CORS (URL Mini App + web.telegram.org) | Для production |
| `ALLOWED_USER_IDS` | ID пользователей через запятую (пустое = доступ всем) | Нет |
| `DATABASE_URL` | Connection string PostgreSQL (`postgresql://user:pass@host:port/db`). Если пусто — in-memory fallback | Для кэшей L2 |
| `DB_POOL_MIN` / `DB_POOL_MAX` | Размеры пула соединений (по умолчанию `1` / `5`) | Нет |
| `DB_CONNECT_TIMEOUT` | Таймаут подключения к БД в секундах (по умолчанию `10`) | Нет |
| `CARDS_CACHE_TTL_SECONDS` | TTL кэша карточек ДТП в PostgreSQL (по умолчанию `86400` = 24ч) | Нет |
| `CLUSTERS_CACHE_TTL_SECONDS` | TTL кэша кластеров в PostgreSQL (по умолчанию `86400` = 24ч) | Нет |
| `EXCEL_CACHE_TTL_SECONDS` | TTL кэша Excel-файлов в PostgreSQL (по умолчанию `86400` = 24ч) | Нет |
| `LLM_API_KEY` | API-ключ [ZhipuAI](https://open.bigmodel.cn) для AI-анализа | Нет |
| `LLM_MODEL` | Модель GLM (по умолчанию `glm-4.7-flash`) | Нет |
| `ENABLE_NEWS_SEARCH` | Поиск новостей для контекста LLM (`true`/`false`, по умолчанию `true`) | Нет |
| `REGIONS_API_ENABLED` | Запрос справочника регионов через API ГИБДД (`1`/`0`, по умолчанию `0` — сразу файловый кэш) | Нет |
| `TARGET_API_TIMEOUT` | Таймаут запросов к API ГИБДД в секундах (по умолчанию `120`) | Нет |
| `LOG_LEVEL` | Уровень логирования (по умолчанию `INFO`) | Нет |
| `CAMERA_DATA_DIR` | Путь к директории с кэшем камер и регионов (по умолчанию `data`) | Нет |

### 3. Запуск

**Production (bothost):**
```bash
python main.py
```
FastAPI поднимается на `$PORT`, Telegram-бот работает в webhook-режиме на `/bot/webhook`, Mini App доступен на `/app/`.

**Локальная разработка:**
```bash
# Терминал 1 — backend + bot (hot reload)
PORT=8080 BOTHOST_DOMAIN=localhost TELEGRAM_BOT_TOKEN=<token> python main.py

# Терминал 2 — frontend (Vite dev server с hot reload)
cd miniapp/frontend
npm run dev
# → http://localhost:5173, проксирует /api → localhost:8080
```

**Только Telegram-бот (polling, без Mini App):**
```bash
python bot.py
```

Либо на Windows: `run_bot.bat` или `start_bot.bat`.

### 4. Установка webhook (после первого деплоя)

```bash
curl "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook?url=https://<BOTHOST_DOMAIN>/bot/webhook"
```

### 5. Настройка Mini App в BotFather

1. Откройте @BotFather → `/newapp` (или `/setmenubutton`)
2. Укажите бота, название «ДТП Статистика»
3. URL: `https://<BOTHOST_DOMAIN>/app/`
4. Теперь у бота появится кнопка-меню, открывающая Mini App

---

## Команды бота

| Команда | Описание |
|---|---|
| `/start` | Приветственное сообщение с описанием |
| `/help` | Справка по форматам запросов и командам |
| `/dtp` | Начать выгрузку через inline-кнопки (регион → период) |
| `/regions` | Показать список всех регионов с кодами |
| `/miniapp` | Прислать inline-кнопку для открытия Mini App |

### Форматы запросов

| Формат | Пример | Описание |
|---|---|---|
| Естественный язык | `Вологодская область за 2025 год` | Автопарсинг региона и периода |
| Естественный язык | `Алтайский край за I квартал 2025` | Кварталы, полугодия, месяцы |
| Строгий формат | `2.2024 1119` | `месяц.год код_региона` |
| С coordinate-прикреплением | Отправка геолокации | Статистика ДТП в радиусе от точки |
| С Excel-файлом | Прикрепить `.xls` / `.xlsx` | Загрузка реестра камер региона |

---

## Структура проекта

```
gibdd-bot/
├── main.py                 ← Единая точка входа: FastAPI + bot (webhook)
├── bot.py                  ← Telegram-бот (polling): команды, inline-кнопки,
│                              пайплайн выгрузки, отправка файлов
├── api_client.py           ← HTTP-клиент для Open Data API stat.gibdd.ru
│                              (connection pooling, ретраи с backoff, throttle)
├── web_fallback.py         ← Fallback-выгрузка через сайт (POST→GET→XML parse)
├── gibdd_parser.py         ← Парсинг карточек ДТП → структура для Excel
├── excel_generator.py      ← Генерация стилизованных .xlsx файлов
├── user_request_parser.py  ← NLP-парсер: естественный язык → регион + период
├── config.py               ← Конфигурация из .env / переменных окружения
├── analytics.py            ← Аналитика: метрики, сравнение периодов, 25 кросс-таблиц,
│                              статистические метрики (severity, Z-score, χ²)
├── report_generator.py     ← Генератор HTML-отчётов: карты (Leaflet),
│                              аналитика (ECharts), очаги, точка с радиусом.
│                              Кластеризация, spiderfy, линейка, попапы.
│                              Библиотеки встраиваются инлайн (кэш в data/report_libs/)
├── concentration_points.py ← Расчёт очагов концентрации ДТП
│                              (НП + вне НП, OSM Overpass, Shapely, методология v2)
├── camera_loader.py        ← Парсинг и поиск камер фотовидеофиксации
├── camera_matcher.py       ← Сопоставление камер с очагами ДТП
├── camera_cache.py         ← Файловый кэш камер по регионам
├── point_statistics.py     ← Локальная статистика по географической точке
├── llm_analyzer.py         ← Интеграция с ZhipuAI GLM (резюме, Q&A, промпты)
├── news_fetcher.py         ← Поиск новостей (Google News RSS + DuckDuckGo)
├── regions_cache.py        ← Файловый кэш справочника регионов
├── regions_builtin.py      ← Встроенный справочник 82 регионов (fallback)
├── regions_builtin.json    ← Тот же справочник в JSON
├── parse_regions.py        ← Утилита парсинга регионов с сайта stat.gibdd.ru
├── np_bdd/                 ← Национальный проект «Безопасные качественные дороги»
│   ├── scripts/            ← forecast.py, embedded_data.py, gibdd_adapter.py, ...
│   ├── datasets/           ← history, plans, vehicles, seasonal (по регионам)
│   └── schemas/            ← JSON Schema для валидации
├── miniapp/                ← Telegram Mini App
│   ├── backend/            ← FastAPI sub-app (монтируется на /api)
│   │   ├── main.py         ← Точка входа FastAPI
│   │   ├── config.py       ← Pydantic-settings
│   │   ├── telegram_auth.py← Проверка подписи initData (HMAC-SHA256)
│   │   ├── routers/        ← regions, parse, dtp, point, cameras, analyze, np_bdd
│   │   ├── services/
│   │   │   ├── gibdd_service.py  ← Мост к модулям gibdd-bot
│   │   │   └── np_bdd_service.py ← Сервис НП БДД
│   │   └── db/             ← Слой PostgreSQL (Этап 2-5)
│   │       ├── connection.py     ← Async-пул (psycopg), init_pool/close_pool/health_check
│   │       ├── schema.sql        ← CREATE TABLE IF NOT EXISTS: tasks, access_log,
│   │       │                       dtp_cards_cache, clusters_cache, excel_cache
│   │       ├── repository.py     ← TaskRepository: save/load/list/delete, log_access
│   │       ├── cards_cache.py    ← L2-кэш карточек ДТП (Этап 3)
│   │       ├── clusters_cache.py ← L2-кэш кластеров + raw_clusters (Этап 4)
│   │       └── excel_cache.py    ← L2-кэш Excel-файлов (BYTEA, Этап 5)
│   └── frontend/           ← Vite + React + TypeScript + Tailwind
│       ├── src/
│       │   ├── App.tsx     ← Главный layout с табами
│       │   ├── lib/        ← telegram.ts, api.ts, utils.ts
│       │   ├── hooks/      ← useTaskPolling.ts, useAnalysisPolling.ts
│       │   └── components/ ← ClustersView, LLMAnalysisView, AnalyticsView,
│       │                     NpBddView, PointStatsView, StructuredForm, ...
│       └── dist/           ← Собранная ститика (после npm run build)
├── data/                   ← Рабочая директория (кэш камер, регионов, OSM)
├── Dockerfile              ← Multi-stage: build frontend + Python runtime
├── requirements.txt        ← Зависимости Python
├── .env.example            ← Шаблон конфигурации
├── install.bat             ← Установка зависимостей (Windows)
├── start_bot.bat           ← Запуск бота (Windows)
└── run_bot.bat             ← Запуск бота с автоустановкой (Windows)
```

---

## API Endpoints

### Mini App (FastAPI на `/api`)

| Метод | Путь | Описание |
|---|---|---|
| GET | `/health` | Health-check (статус бота, версия, БД) |
| GET | `/health/db` | Детальная диагностика PostgreSQL (пул, latency, schema) |
| GET | `/health/db/cards` | Статистика cards_cache: записи, hits/misses, размер |
| GET | `/health/db/clusters` | Статистика clusters_cache: записи, hits/misses, размер |
| GET | `/health/db/excel` | Статистика excel_cache: записи, hits/misses, размер |
| GET | `/api/regions` | Список регионов с кодами |
| GET | `/api/regions/search?q=` | Поиск регионов (autocomplete) |
| POST | `/api/parse` | Парсинг естественного языка → `{region_code, period}` |
| POST | `/api/dtp/tasks` | Создать задачу выгрузки, вернуть `task_id` |
| GET | `/api/dtp/tasks` | Список задач пользователя |
| GET | `/api/dtp/tasks/{id}` | Статус задачи (для polling) |
| GET | `/api/dtp/tasks/{id}/files` | Список готовых файлов |
| GET | `/api/dtp/tasks/{id}/map` | HTML-карта ДТП (для iframe) |
| GET | `/api/dtp/tasks/{id}/download/{file_type}` | Скачать Excel/HTML |
| POST | `/api/dtp/tasks/{id}/clusters` | Запустить расчёт очагов |
| GET | `/api/dtp/tasks/{id}/clusters?wait=N` | Статус очагов (long polling) |
| GET | `/api/dtp/tasks/{id}/clusters/map` | HTML-карта очагов |
| GET | `/api/dtp/tasks/{id}/clusters/excel` | Скачать Excel по очагам (4 листа) |
| POST | `/api/dtp/tasks/{id}/point` | Статистика ДТП в радиусе от точки |
| GET | `/api/dtp/tasks/{id}/point/map` | HTML-карта точки |
| GET | `/api/dtp/tasks/{id}/point/excel` | Excel по точке (2 листа) |
| GET | `/api/dtp/tasks/{id}/llm/providers` | Доступные LLM-провайдеры (free/paid) |
| POST | `/api/dtp/tasks/{id}/llm/summary` | Запустить генерацию резюме |
| GET | `/api/dtp/tasks/{id}/llm/summary?wait=N` | Статус резюме (long polling) |
| POST | `/api/dtp/tasks/{id}/llm/ask` | Задать вопрос нейросети |
| GET | `/api/dtp/tasks/{id}/llm/qa-history` | История Q&A |
| GET | `/api/cameras` | Список регионов с камерами |
| POST | `/api/cameras/{reg_code}` | Загрузить реестр камер региона |
| DELETE | `/api/cameras/{reg_code}` | Удалить реестр камер |
| GET | `/api/np-bdd/regions` | Регионы НП БДД |
| GET | `/api/np-bdd/data` | Данные НП БДД (история, прогноз, KPI) |
| GET/PATCH | `/api/np-bdd/settings` | Настройки (plan_line_mode, forecast_method) |
| POST | `/api/np-bdd/freeze` | Заморозить год |
| POST | `/api/np-bdd/unfreeze` | Разморозить год |
| GET | `/api/np-bdd/frozen` | Список замороженных лет |

### Telegram-бот

| Эндпоинт | Метод | Назначение |
|---|---|---|
| `/bot/webhook` | POST | Webhook Telegram (приём updates) |

Документация Swagger UI: `https://<BOTHOST_DOMAIN>/docs` или `http://localhost:8080/docs`

---

## Деплой

Подробная инструкция по деплою на bothost.ru — в [`README_DEPLOY_BOTHOST.md`](README_DEPLOY_BOTHOST.md).

### Краткая сводка

| Платформа | Способ | Главный файл |
|---|---|---|
| bothost.ru (рекомендуется) | Dockerfile | `main.py` |
| Timeweb Cloud / Selectel | docker-compose | `main.py` |
| Локальная разработка | uvicorn + Vite | `main.py` + `npm run dev` |
| Только Telegram-бот | polling | `bot.py` |

### Проверка работоспособности

| Endpoint | Что проверяет | Ожидаемый ответ |
|----------|---------------|-----------------|
| `https://<DOMAIN>/health` | Сервер жив, БД готова | `{"status":"ok","database":{"ready":true}}` |
| `https://<DOMAIN>/health/db` | Диагностика PostgreSQL | `pool`, `latency_ms`, `tables` |
| `https://<DOMAIN>/health/db/excel` | Статистика excel_cache | `entries`, `hits`, `misses` |
| `https://<DOMAIN>/api/regions` | Авторизация | 401 (нужен initData) |
| `https://<DOMAIN>/app/` | Frontend | HTML страница |
| `https://<DOMAIN>/docs` | Swagger UI | Документация API |
| Telegram `/start` | Бот отвечает | Сообщение приветствия |
| Telegram `/miniapp` | Кнопка Mini App | Inline-кнопка «Открыть» |

---

## Зависимости

| Пакет | Версия | Назначение |
|---|---|---|
| `python-telegram-bot` | 21.7 | Telegram Bot API |
| `fastapi` | 0.115+ | Web-фреймворк для Mini App |
| `uvicorn` | 0.30+ | ASGI-сервер |
| `httpx` | 0.27.0 | Асинхронный HTTP-клиент |
| `openpyxl` | 3.1.5 | Генерация Excel-файлов |
| `xlrd` | 2.0.1 | Чтение .xls файлов (реестр камер) |
| `python-dotenv` | 1.0.1 | Загрузка .env |
| `shapely` | 2.0.6 | Геометрические операции (полигоны НП) |
| `pydantic-settings` | 2.x | Конфигурация Mini App |
| `psycopg[binary,pool]` | 3.2+ | Async-драйвер PostgreSQL + пул соединений (Этап 2-5) |
| `pytz` | 2024.x | Таймзоны для НП БДД |

Опционально (для AI-анализа):
| Пакет | Назначение |
|---|---|
| `zhipuai` | SDK для ZhipuAI GLM-4.7-Flash |

Frontend:
| Пакет | Назначение |
|---|---|
| `react` 18 + `react-dom` | UI-фреймворк |
| `typescript` 5 | Типизация |
| `vite` 5 | Сборщик + dev-сервер |
| `tailwindcss` 3 | Утилитарный CSS |
| `@tanstack/react-query` 5 | Серверный стейт (polling, кэш) |

---

## Разработка

### Обновление встроенного справочника регионов

```bash
python parse_regions.py
```

Скрипт загружает страницу stat.gibdd.ru, извлекает `var regions` и `var regId2MiasId` из HTML, конвертирует в API-формат (`"11" + miasId`) и перегенерирует `regions_builtin.py` и `regions_builtin.json`.

### Smoke-тесты

| Скрипт | Что тестирует |
|---|---|
| `scripts/smoke_test_new_match_clusters.py` | Методология v2 сопоставления очагов (15 тестов) |
| `scripts/smoke_test_bdd_vehicle.py` | Аналитика по профилю ТС (БДД) |
| `scripts/smoke_test_stage1_cross_tables.py` | Кросс-таблицы (Этап 1) |
| `scripts/smoke_test_stage2_stats.py` | Статистические метрики (Этап 2) |
| `scripts/smoke_test_current_month.py` | Прогноз НП БДД по текущему месяцу |

### Структура директории `data/`

Создаётся автоматически при первом запуске:

```
data/
├── regions_cache.json          # Кэш справочника регионов
├── report_libs/                # Кэш CDN-библиотек для HTML-карт
│   ├── leaflet.css             # Leaflet (загружается с unpkg.com при первом запуске)
│   ├── leaflet.js
│   ├── MarkerCluster.css       # MarkerCluster + Default + JS
│   └── echarts.min.js          # ECharts (для аналитических отчётов)
├── osm_cache/                  # Кэш границ НП из OSM (по регионам)
│   ├── region_1119.json
│   ├── settlements_*.json
│   └── ...
├── cameras_1119.xls            # Кэш реестра камер региона 1119
├── cameras_1101.xls            # Кэш реестра камер региона 1101
└── ...
```

### Сборка frontend для деплоя

```bash
cd miniapp/frontend
npm install
npm run build
# Результат: miniapp/frontend/dist/
```

---

## Требования 152-ФЗ

⚠️ Mini App обрабатывает ПДн (данные участников ДТП). Для соответствия 152-ФЗ:

1. **Хостинг в РФ**: bothost.ru / Timeweb Cloud / Selectel / Beget (все в реестре Минцифры)
2. **TLS обязателен** (Let's Encrypt — бесплатно, на bothost включён автоматически)
3. **Политика обработки ПДн** + **Согласие** при первом открытии Mini App
4. **Уведомление Роскомнадзора** об обработке ПДн (через Госуслуги)
5. **Журнал аудита доступа** к ПДн (логировать все запросы `user_id → region_code, period`)
6. **Шифрование БД при rest** (LUKS для диска VPS, на bothost — на стороне хостера)

---

## Устранение неполадок

### Бот не отвечает после деплоя

1. Проверьте `/health` — `telegram_bot` должен быть `"running"`
2. Если `"stopped"` — проверьте логи на `InvalidToken` или проблемы с webhook
3. Проверьте webhook:
   ```bash
   curl "https://api.telegram.org/bot<TOKEN>/getWebhookInfo"
   ```
4. Если `last_error_message` указывает на 404/503 — проверьте `BOTHOST_DOMAIN` и что `/bot/webhook` доступен

### Mini App открывается, но API возвращает 401

`X-Tg-Init-Data` не передаётся. Возможные причины:
1. Frontend собран со старым `VITE_API_BASE` — пересоберите
2. Telegram SDK не загрузился — проверьте `<script src="https://telegram.org/js/telegram-web-app.js">` в `index.html`
3. Mini App открыт в обычном браузере (не через Telegram) — `initData` пустой

### CORS ошибки в консоли браузера

Добавьте ваш домен в `CORS_ORIGINS`:
```
CORS_ORIGINS=https://bot1234.bothost.tech,https://web.telegram.org,https://a.telegram.org
```

### LLM: ошибка `TypeError: ... unexpected keyword argument 'max_retries'`

Серверная версия `llm_analyzer.py` устарела. Обновите файл на сервере из репозитория — `get_ai_summary` и `get_ai_answer` теперь принимают `max_retries: int = 3`.

### LLM: 429 (rate limit) после retry

Кнопка «Повторить» теперь сбрасывает кэш react-query (`queryClient.removeQueries`) и показывает прогресс-бар мгновенно. После успешного retry результат возвращается в MiniApp через long polling автоматически.

### Frontend не обновляется после деплоя

Vite добавляет хэш к именам файлов (`index-AbCd1234.js`). Если старый `index.html` закеширован — он будет ссылаться на несуществующий файл. Решение: убедитесь, что bothost не кэширует `/app/` агрессивно, или добавьте version-busting.

### PostgreSQL: кэш не срабатывает (всегда MISS)

1. Проверьте `DATABASE_URL` в `.env` — без него приложение работает в in-memory режиме
2. Откройте `/health/db` — `ready: true` и `pool` не `null`
3. Проверьте логи на `cards_cache: TTL=86400s` — должно появляться при первом запросе
4. Проверьте, что в логах есть `PUT` и `HIT` записи, а не только `MISS`
5. При нехватке места в БД старые записи могут не очищаться — проверьте `/health/db/excel` на размер `entries`

### PostgreSQL: ConnectionError / pool timeout

1. Проверьте `DB_POOL_MAX` (по умолчанию 5) — при высоком RPS увеличьте
2. Проверьте `DB_CONNECT_TIMEOUT` (по умолчанию 10с)
3. На bothost.ru PostgreSQL иногда уходит на обслуживание — приложение автоматически переключится на in-memory fallback, но кэш будет менее эффективным

---

## Лицензия

MIT
