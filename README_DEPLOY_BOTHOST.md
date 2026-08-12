# Деплой GIBDD Bot + Mini App на bothost.ru

Этот документ описывает деплой единого приложения (Telegram-бот + Mini App)
на хостинг [bothost.ru](https://bothost.ru).

## Режимы деплоя

Проект поддерживает 2 режима, управляемых переменной `DEPLOYMENT_MODE`:

### Режим `single` (по умолчанию, текущий)

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

**Единый процесс**: `main.py` поднимает FastAPI и в lifespan инициализирует
Telegram-бота в webhook-режиме. Mini App frontend (собранный React) раздаётся
как статика из `miniapp/frontend/dist`.

- Потребление RAM: ~300-500 MB
- Достаточно для текущей нагрузки (async/await через asyncio.Semaphore)
- Не требует Redis/Celery

### Режим `multi` (после Фазы C.3, Celery-режим)

```
                    ┌─────────────────────────────────┐
                    │   bothost.ru (TLS-терминация)    │
                    │   bot1234.bothost.tech           │
                    └────────────┬────────────────────┘
                                 │  HTTPS
                    ┌────────────▼────────────────────┐
                    │   supervisord (PID 1)            │
                    ├─────────────────────────────────┤
                    │  ┌──────────────────────────┐   │
                    │  │ redis-server             │   │  maxmemory 128mb
                    │  │ (брокер + result backend)│   │  без AOF
                    │  └────────────┬─────────────┘   │
                    │               │                 │
                    │  ┌────────────▼─────────────┐   │
                    │  │ uvicorn main:app         │   │  /health, /health/redis,
                    │  │ (FastAPI + webhook)      │◄──┤  /health/celery
                    │  └────────────┬─────────────┘   │
                    │               │                 │
                    │  ┌────────────▼─────────────┐   │
                    │  │ celery worker            │   │  4 очереди,
                    │  │ (concurrency=4)          │   │  --max-tasks-per-child=10
                    │  └──────────────────────────┘   │
                    │  ┌──────────────────────────┐   │
                    │  │ celery beat              │   │  cleanup, scheduled tasks
                    │  └──────────────────────────┘   │
                    └─────────────────────────────────┘
```

**Multi-process**: supervisord запускает 4 процесса в одном контейнере.
- Потребление RAM: ~700 MB базовое, ~1.3 GB пиковое
- Полноценный async-пайплайн: LLM, Excel, clusters не блокируют API
- Retry, persistence, scheduled cleanup из коробки (Celery)
- Оптимизировано под bothost 4 vCPU / 2 GB RAM / 15 GB NVMe

## Что изменилось по сравнению с pure-Telegram-ботом

| До | После |
|----|-------|
| `python bot.py` (polling) | `python main.py` (FastAPI + webhook) |
| 1 процесс: только бот | 1 процесс: FastAPI + бот + ститика |
| Нет веб-интерфейса | Mini App на `/app/` |
| Webhook не нужен | Webhook обязателен на `/bot/webhook` |
| `bot._build_app()` — внутренний | `bot._build_app()` — используется `main.py` |

Структура проекта:

```
gibdd-bot/
├── main.py                 ← Единая точка входа для bothost
├── bot.py                  ← Существующий бот + команда /miniapp
├── config.py               ← Существующий конфиг
├── requirements.txt        ← Объединённые зависимости
├── Dockerfile              ← Multi-stage: frontend build + python main.py
├── env.example             ← Шаблон .env с bothost-переменными
├── miniapp/
│   ├── __init__.py         ← Пакет
│   ├── backend/
│   │   ├── main.py         ← FastAPI sub-app (монтируется на /api)
│   │   ├── config.py       ← Settings (pydantic-settings)
│   │   ├── telegram_auth.py← Проверка initData (HMAC-SHA256)
│   │   ├── routers/        ← /regions, /parse, /dtp, /point
│   │   └── services/
│   │       └── gibdd_service.py ← Мост к существующим модулям gibdd-bot
│   └── frontend/           ← Vite + React + TS + Tailwind
│       └── dist/           ← Собранная ститика (после npm run build)
└── ... (существующие модули gibdd-bot)
```

## Подготовка к деплою

### 1. Получите домен на bothost

После регистрации бота на bothost вы получите домен вида
`bot1234.bothost.tech`. Запишите его.

### 2. Подготовьте переменные окружения

Скопируйте `env.example` в `.env` и заполните:

```bash
cp env.example .env
```

Обязательные переменные:

| Переменная | Пример | Описание |
|------------|--------|----------|
| `TELEGRAM_BOT_TOKEN` | `123456:ABC-DEF...` | Токен от @BotFather |
| `BOTHOST_DOMAIN` | `bot1234.bothost.tech` | Домен от bothost |
| `PORT` | `8080` | Порт (bothost обычно передаёт через `$PORT`) |
| `CORS_ORIGINS` | `https://bot1234.bothost.tech,https://web.telegram.org` | CORS |

Опциональные:

| Переменная | Описание |
|------------|----------|
| `LLM_API_KEY` | Ключ ZhipuAI для AI-анализа |
| `ALLOWED_USER_IDS` | Список Telegram ID через запятую (пусто = всем) |

### 3. Соберите frontend (если не используете Docker)

Если bothost собирает Dockerfile автоматически — этот шаг выполняется
внутри контейнера. Если деплоите как Python-процесс:

```bash
cd miniapp/frontend
npm install
npm run build
# Результат: miniapp/frontend/dist/
```

## Деплой на bothost.ru

### Вариант A: Через Dockerfile, режим `single` (по умолчанию)

1. Загрузите репозиторий на bothost (через git или архивом).
2. В настройках проекта укажите **Dockerfile** как источник.
3. В переменных окружения bothost задайте `TELEGRAM_BOT_TOKEN`,
   `BOTHOST_DOMAIN`, `CORS_ORIGINS`.
4. `DEPLOYMENT_MODE` не задавайте (или задайте `single`).
5. bothost автоматически:
   - Соберёт frontend (Stage 1: `node:20-alpine`)
   - Установит Python-зависимости (Stage 2: `python:3.11-slim`)
   - Запустит `python main.py` на `$PORT` (через entrypoint.sh)

### Вариант B: Через Dockerfile, режим `multi` (Celery + Redis)

1. Загрузите репозиторий на bothost (через git или архивом).
2. В настройках проекта укажите **Dockerfile** как источник.
3. В переменных окружения bothost задайте:
   - `TELEGRAM_BOT_TOKEN`
   - `BOTHOST_DOMAIN`
   - `CORS_ORIGINS`
   - **`DEPLOYMENT_MODE=multi`** (ключевая переменная)
   - `USE_CELERY=true` (по умолчанию уже true, но лучше явно)
4. bothost соберёт образ и запустит `entrypoint.sh`, который:
   - Видит `DEPLOYMENT_MODE=multi` → запускает `supervisord -n -c /etc/supervisord.conf`
   - supervisord поднимает 4 процесса: redis, api, worker, beat
5. Проверка:
   - `https://<DOMAIN>/health` → статус API
   - `https://<DOMAIN>/health/redis` → `connected: true`, `latency_ms < 5`
   - `https://<DOMAIN>/health/celery` → `ping_count >= 1`, `workers: ["celery@<hostname>"]`

**Важно для multi-режима:**
- RAM сервера должен быть ≥ 2 ГБ (пиковое потребление ~1.3 ГБ + запас на OS)
- CPU ≥ 4 vCPU (worker concurrency=4)
- /data должен быть persistent volume (для Redis RDB, если включён)

### Вариант C: Через главный файл main.py (fallback, только single-режим)

Если bothost не поддерживает Dockerfile:

1. Укажите `main.py` как главный файл в настройках bothost.
2. Убедитесь, что `requirements.txt` указан как файл зависимостей.
3. **Соберите frontend локально** и загрузите `miniapp/frontend/dist/`
   вместе с проектом.
4. bothost запустит `python main.py`.

## После первого деплоя: установка webhook

После успешного запуска (проверьте `/health` в браузере) установите
webhook для Telegram **один раз**:

```bash
curl "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook?url=https://<BOTHOST_DOMAIN>/bot/webhook"
```

Проверка:

```bash
curl "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/getWebhookInfo"
```

В ответе `webhook_url` должен быть `https://<BOTHOST_DOMAIN>/bot/webhook`,
а `last_error_message` — пустым.

## Настройка Mini App в BotFather

Чтобы кнопка Mini App отображалась в меню бота:

1. Откройте @BotFather → `/newapp` (или `/setmenubutton`).
2. Укажите бота, название "ДТП Статистика".
3. URL: `https://<BOTHOST_DOMAIN>/app/`.
4. Теперь у бота появится кнопка-меню (слева от поля ввода), открывающая
   Mini App.

Альтернативно — команда `/miniapp` в чате с ботом присылает inline-кнопку
для открытия Mini App.

## Проверка работоспособности

| Endpoint | Что проверяет | Ожидаемый ответ |
|----------|---------------|-----------------|
| `https://<DOMAIN>/health` | Сервер жив | `{"status":"ok",...}` |
| `https://<DOMAIN>/` | Корневой info | JSON с путями |
| `https://<DOMAIN>/api/miniapp/health` | Mini App API | `{"status":"ok",...}` |
| `https://<DOMAIN>/api/regions` | Авторизация | 401 (нужен initData) |
| `https://<DOMAIN>/app/` | Frontend | HTML страница |
| `https://<DOMAIN>/docs` | Swagger UI | Документация API |
| Telegram `/start` | Бот отвечает | Сообщение приветствия |
| Telegram `/miniapp` | Кнопка Mini App | Inline-кнопка "Открыть" |

## Локальная разработка

### Backend + Frontend (hot reload)

Терминал 1 — backend:

```bash
PORT=8080 BOTHOST_DOMAIN=localhost TELEGRAM_BOT_TOKEN=<token> python main.py
```

Терминал 2 — frontend (dev-сервер с hot reload):

```bash
cd miniapp/frontend
npm run dev
# Откроется http://localhost:5173, проксирует /api → localhost:8080
```

### Только backend (frontend уже собран)

```bash
cd miniapp/frontend && npm run build && cd ../..
PORT=8080 BOTHOST_DOMAIN=localhost TELEGRAM_BOT_TOKEN=<token> python main.py
# Откройте http://localhost:8080/app/
```

### Без Telegram-бота (только Mini App)

Можно запустить с пустым `TELEGRAM_BOT_TOKEN` — FastAPI поднимется,
Mini App будет работать, но авторизация через initData не пройдёт
(нужен реальный токен для проверки подписи).

## Устранение неполадок

### Бот не отвечает после деплоя

1. Проверьте `/health` — `telegram_bot` должен быть `"running"`.
2. Если `"stopped"` — смотрите логи bothost, обычно причина:
   - Невалидный `TELEGRAM_BOT_TOKEN`
   - Telegram API недоступен (проверьте `getWebhookInfo`)
3. Проверьте, что webhook установлен:
   ```bash
   curl "https://api.telegram.org/bot<TOKEN>/getWebhookInfo"
   ```
4. Если `last_error_message` указывает на 404/503 — проверьте, что
   `BOTHOST_DOMAIN` задан и `/bot/webhook` доступен.

### Mini App открывается, но API возвращает 401

Это значит, что `X-Tg-Init-Data` не передаётся. Возможные причины:

1. Frontend собран со старым `VITE_API_BASE` — пересоберите.
2. Telegram SDK не загрузился — проверьте, что в `index.html` есть
   `<script src="https://telegram.org/js/telegram-web-app.js"></script>`.
3. Mini App открыт в обычном браузере (не через Telegram) — в этом
   случае `initData` пустой, авторизация не пройдёт.

### CORS ошибки в консоли браузера

Добавьте ваш домен в `CORS_ORIGINS`:

```
CORS_ORIGINS=https://bot1234.bothost.tech,https://web.telegram.org,https://a.telegram.org
```

### Frontend не обновляется после деплоя

Vite добавляет хэш к именам файлов (`index-AbCd1234.js`). Если старый
`index.html` закеширован — он будет ссылаться на несуществующий файл.
Решение: убедитесь, что bothost не кэширует `/app/` агрессивно, или
добавьте version-busting.

### Ошибка `InvalidToken` в логах

Telegram отверг токен. Проверьте:

1. Токен скопирован полностью (включая двоеточие и часть после).
2. Токен не отозван в @BotFather (`/revoke` + новый токен).
3. Нет лишних пробелов/переводов строк в `.env`.

### Multi-режим: `/health/redis` возвращает `connected: false`

Если вы переключились в `DEPLOYMENT_MODE=multi`, но Redis не отвечает:

1. Проверьте, что контейнер запущен и supervisord работает:
   ```
   docker exec <container> supervisorctl status
   ```
   Ожидаемый вывод (все `RUNNING`):
   ```
   api       RUNNING   pid 123, uptime 0:05:30
   beat      RUNNING   pid 124, uptime 0:05:30
   redis     RUNNING   pid 125, uptime 0:05:30
   worker    RUNNING   pid 126, uptime 0:05:30
   ```

2. Если `redis` в статусе `FATAL` или `STOPPED`:
   ```
   docker exec <container> supervisorctl tail redis stderr
   ```
   Частая причина — отсутствие директории `/data/redis` (создаётся в entrypoint,
   но если volume подключен позже — может быть пустой).

3. Если `redis` RUNNING, но `/health/redis` всё равно `connected: false`:
   - Проверьте `REDIS_URL` в env: должен быть `redis://127.0.0.1:6379/0`
     (внутри контейнера, не `redis://redis:6379` — это для docker-compose)
   - supervisord.conf уже передаёт правильный `REDIS_URL` в [program:api],
     но если он переопределён в bothost env — приоритет у bothost.

### Multi-режим: `/health/celery` возвращает `ping_count: 0`

Worker не отвечает на ping. Возможные причины:

1. Worker ещё стартует (повторите через 30 сек).
2. Worker упал — проверьте:
   ```
   docker exec <container> supervisorctl tail worker stderr
   ```
3. Если в логах `ModuleNotFoundError: No module named 'worker'`:
   - Код `worker/` не попал в образ.
   - Проверьте, что `.dockerignore` не исключает `worker/`.

### Multi-режим: OOM kill (контейнер падает по памяти)

Признаки: контейнер рестартуется, в логах host'а `OOM killed`.

Причины и решения:
- Уменьшите `CELERY_WORKER_CONCURRENCY` до 2 (в bothost env):
  ```
  CELERY_WORKER_CONCURRENCY=2
  ```
  Но учтите: это переменная Celery app, для supervisor-деплоя нужно
  отредактировать `docker/supervisord.conf` → секция `[program:worker]` →
  `--concurrency=2`.
- Уменьшите `CELERY_MAX_TASKS_PER_CHILD` до 5 (перезапуск чаще).
- Перейдите на `DEPLOYMENT_MODE=single` — если Celery пока не нужен.

## Ограничения bothost.ru

- **1 контейнер**: bothost запускает один Docker-контейнер.
  - В режиме `single`: 1 процесс (`python main.py`)
  - В режиме `multi`: supervisord с 4 процессами (redis, api, worker, beat)
- **RAM ~2 ГБ**: gibdd-bot уже оптимизирован под это.
  - `single` режим: ~300-500 MB (запас ~1.5 ГБ)
  - `multi` режим: ~700 MB базовое, ~1.3 GB пиковое (запас ~700 MB)
  - См. комментарии в `bot.py` про tracemalloc.
- **CPU 4 vCPU**: достаточно для multi-режима (worker concurrency=4)
- **Диск 15 ГБ NVMe**:
  - Кэш регионов/камер хранится в `data/`. На bothost обычно
    `/data` — задайте `CAMERA_DATA_DIR=/data` если доступно.
  - В multi-режиме Redis хранит данные в `/data/redis/` (если включены RDB snapshots;
    по умолчанию отключено для экономии диска и RAM).
- **Таймауты**: API ГИБДД может отвечать до 120 сек. `TARGET_API_TIMEOUT=120`
  уже настроен. bothost обычно даёт 300 сек на HTTP-запрос.
- **152-ФЗ**: bothost — российский хостинг, дата-центр в РФ. Данные
  пользователей не покидают юрисдикцию.

## Sprint 7 / Фаза C.2.4 — feature flag `GIBDD_USE_CORE_PIPELINE`

В **single**-режиме (текущий bothost) pipeline.execute_task работает через
прямые вызовы `gibdd_parser` / `analytics` / `excel_generator` / `report_generator`.
Это **legacy path**, по умолчанию включён.

Чтобы переключить FastAPI-путь на использование тех же `miniapp.backend.core.*`
sync-функций, которые будет вызывать будущий Celery-worker (Фаза C.3):

```bash
# В Variables bothost:
GIBDD_USE_CORE_PIPELINE=1
```

После рестарта контейнера в логах появится строка:
```
Task <id>: execute_task started (path=core)
Task <id>: PARSING via core/build_excel_data_sync
Task <id>: ANALYTICS via core/build_analytics_sync
Task <id>: GENERATING Excel via core/generate_excel_bytes_sync
Task <id>: GENERATING map via core/generate_map_html_sync
```

**Поведение идентично** legacy path — те же модули вызываются под капотом
(`core/build_excel_data_sync` → `gibdd_parser.build_file1/2_data`,
`core/generate_excel_bytes_sync` → `excel_generator.generate_both_files`, ...).

Разница — только в точке входа: `core/` даёт unified API для будущего Celery-path.

**FETCHING** остаётся async-native в обоих путях (`bot._fetch_cards_for_period`).
Причина: `core/fetch_cards_for_period_sync` использует `asyncio.run()` внутри,
что конфликтует с running FastAPI event loop. Celery worker (sync context)
будет использовать sync-обёртку нормально.

**Rollback**: `GIBDD_USE_CORE_PIPELINE=0` (или удалите переменную) — мгновенный
возврат на legacy path без передеплоя кода, только рестарт контейнера.

## Откат к polling-режиму

Если нужно вернуться к старому режиму (только Telegram-бот без Mini App):

```bash
# 1. Удалите webhook
curl "https://api.telegram.org/bot<TOKEN>/deleteWebhook"

# 2. Запустите старый main.py (переименован в bot.py)
python bot.py
```

`bot.py` полностью сохранил свою polling-логику и может работать
независимо от `main.py` и Mini App.
