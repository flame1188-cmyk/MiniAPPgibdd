"""
Единая точка входа для bothost.ru: FastAPI + Telegram bot (webhook mode).

Структура:
- FastAPI запускается на порту из $PORT (bothost отдаёт через env)
- Telegram-бот работает в webhook-режиме на /bot/webhook
- Mini App frontend раздаётся из /app (после `npm run build` в miniapp/frontend)
- Существующие модули gibdd-bot импортируются напрямую (мы в корне проекта)

Запуск локально (для разработки):
    PORT=8080 python main.py

Запуск на bothost:
    Главный файл в настройках bothost: main.py
    Переменные окружения: см. .env.example

После первого деплоя установите webhook:
    curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://<BOTHOST_DOMAIN>/bot/webhook"
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import status
from fastapi.responses import JSONResponse

# Убеждаемся, что корень gibdd-bot в sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Импортируем существующий конфиг gibdd-bot
from config import (
    TELEGRAM_BOT_TOKEN,
    ALLOWED_USER_IDS,
    LLM_API_KEY,
    LOG_LEVEL,
    validate_config,
)

# Настраиваем логирование ДО остальных импортов
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# FastAPI
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

# Telegram
from telegram import Update
from telegram.ext import Application

# Mini App backend
from miniapp.backend.main import app as miniapp_app
from miniapp.backend.config import settings as miniapp_settings
from miniapp.backend.db.connection import (
    init_pool as db_init_pool,
    close_pool as db_close_pool,
    is_db_ready as db_is_ready,
)


# ============================================================
# Константы
# ============================================================
PORT = int(os.environ.get("PORT", "8080"))
# Нормализуем домен: убираем возможные протоколы/слэши/порт,
# которые пользователь мог случайно добавить в BOTHOST_DOMAIN.
# Например: "https://bot1234.bothost.tech/" → "bot1234.bothost.tech"
_raw_domain = os.environ.get("BOTHOST_DOMAIN", "").strip()
for _proto in ("https://", "http://", "www."):
    if _raw_domain.startswith(_proto):
        _raw_domain = _raw_domain[len(_proto):]
BOTHOST_DOMAIN = _raw_domain.rstrip("/").split(":")[0]  # отбрасываем порт, если есть
WEBHOOK_PATH = "/bot/webhook"
WEBHOOK_URL = f"https://{BOTHOST_DOMAIN}{WEBHOOK_PATH}" if BOTHOST_DOMAIN else ""

# Путь к собранному фронтенду (после `npm run build`)
FRONTEND_DIST = _PROJECT_ROOT / "miniapp" / "frontend" / "dist"


# ============================================================
# Создание Telegram Application (через существующий bot._build_app)
# ============================================================
def _create_telegram_app() -> Application:
    """
    Создаёт Telegram Application, переиспользуя существующую
    фабрику _build_app() из bot.py.

    bot.py уже настроил все handler'ы (start, help, dtp, regions,
    callback_query, текстовые сообщения, локации, документы, error_handler).
    """
    import bot as bot_module

    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN не задан. Укажите его в .env"
        )

    # Используем существующую фабрику
    app = bot_module._build_app(TELEGRAM_BOT_TOKEN)
    logger.info("Telegram Application создан (через bot._build_app)")
    return app


async def _set_bot_commands(app: Application) -> None:
    """Устанавливает меню команд бота (видно в /menu и при вводе /)."""
    from telegram import BotCommand

    commands = [
        BotCommand("start", "Запустить бота"),
        BotCommand("dtp", "Выгрузка ДТП через кнопки"),
        BotCommand("miniapp", "Открыть веб-приложение"),
        BotCommand("regions", "Список регионов"),
        BotCommand("help", "Справка"),
    ]
    try:
        await app.bot.set_my_commands(commands)
        logger.info("Меню команд бота установлено")
    except Exception as exc:
        logger.warning(f"Не удалось установить меню команд: {exc}")


async def _register_telegram_webhook() -> None:
    """
    Регистрирует webhook в Telegram через Bot API setWebhook.

    Использует прямой HTTP-запрос к api.telegram.org (через httpx),
    а не PTB updater — поэтому не требует extra `python-telegram-bot[webhooks]`.

    Telegram будет слать POST /bot/webhook на наш BOTHOST_DOMAIN,
    FastAPI принимает его и передаёт в tg_app.process_update().
    """
    if not WEBHOOK_URL:
        logger.warning(
            "BOTHOST_DOMAIN не задан — webhook URL не зарегистрирован в Telegram. "
            "Укажите BOTHOST_DOMAIN в .env, либо установите webhook вручную: "
            "curl 'https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://<DOMAIN>/bot/webhook'"
        )
        return

    import httpx

    api_url = (
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook"
    )
    payload = {
        "url": WEBHOOK_URL,
        "allowed_updates": [
            "message",
            "edited_message",
            "callback_query",
            "inline_query",
        ],
        "drop_pending_updates": False,
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(api_url, json=payload)
            data = resp.json()
        if data.get("ok"):
            logger.info(
                f"Telegram webhook зарегистрирован: {WEBHOOK_URL} "
                f"(description: {data.get('description', 'ok')})"
            )
        else:
            logger.error(
                f"setWebhook failed: {data}. "
                f"Webhook URL: {WEBHOOK_URL}"
            )
    except Exception as exc:
        logger.warning(
            f"Не удалось зарегистрировать webhook через API: {exc}. "
            f"Установите вручную: "
            f"curl 'https://api.telegram.org/bot<TOKEN>/setWebhook?url={WEBHOOK_URL}'"
        )


# Глобальный экземпляр Telegram Application
tg_app: Application = None  # type: ignore


# ============================================================
# Lifespan: запуск и остановка Telegram-бота в webhook-режиме
# ============================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Жизненный цикл: запуск Telegram-бота + инициализация Mini App."""
    global tg_app

    # Проверяем конфигурацию
    errors = validate_config()
    if errors:
        logger.error(f"Ошибки конфигурации: {errors}")
        # Не падаем — FastAPI поднимется, но бот работать не будет

    # Создаём директорию для задач
    miniapp_settings.tasks_path.mkdir(parents=True, exist_ok=True)

    # Запускаем Telegram-бота в webhook-режиме через FastAPI endpoint.
    #
    # ВАЖНО: мы НЕ используем tg_app.updater.start_webhook() — он запускает
    # внутренний HTTP-сервер PTB и требует extra `python-telegram-bot[webhooks]`.
    # Вместо этого FastAPI сам принимает POST /bot/webhook и вызывает
    # tg_app.process_update(update). Это стандартный паттерн интеграции
    # PTB + FastAPI, не требующий никаких extras.
    if TELEGRAM_BOT_TOKEN:
        try:
            tg_app = _create_telegram_app()
            # initialize() загружает bot info (get_me) — проверяет токен
            await tg_app.initialize()
            # start() запускает handlers, но НЕ запускает updater/polling
            await tg_app.start()

            # Явно регистрируем webhook в Telegram (FastAPI endpoint уже готов)
            await _register_telegram_webhook()

            # Устанавливаем меню команд бота
            await _set_bot_commands(tg_app)
        except Exception as exc:
            logger.exception(f"Не удалось запустить Telegram-бота: {exc}")
            tg_app = None
    else:
        logger.warning(
            "TELEGRAM_BOT_TOKEN не задан — бот не запущен. "
            "Mini App продолжит работать, но без webhook."
        )

    # Инициализация Mini App — регионы загружаются лениво при первом
    # обращении к /api/regions, чтобы не блокировать старт сервера
    # (API ГИБДД может тормозить с ретраями до 20 сек).
    logger.info("Mini App: стартовая инициализация пропущена — lazy loading")

    # === Инициализация пула PostgreSQL (опционально) ===
    # Если DATABASE_URL задан — создаём пул и применяем схему.
    # Если нет или не удалось подключиться — приложение продолжает работу
    # с in-memory хранилищем (см. db/repository.py).
    try:
        db_ready = await db_init_pool()
        if db_ready:
            logger.info("PostgreSQL: пул готов, задачи и аудит-лог персистятся")
        else:
            logger.info(
                "PostgreSQL: in-memory fallback активирован "
                "(DATABASE_URL не задан или подключение не удалось)"
            )
    except Exception as exc:
        logger.warning(
            f"PostgreSQL init failed: {exc} — продолжаем с in-memory fallback"
        )

    # === Sprint 5: Task recovery на startup ===
    # После рестарта сервера in-flight задачи (status='fetching'/'parsing'/
    # 'analytics'/'generating'/'running') остаются в этом статусе вечно —
    # рабочий процесс, который их обрабатывал, умер. Помечаем их как failed,
    # чтобы пользователь увидел ошибку и мог пересоздать задачу.
    try:
        from miniapp.backend.db.repository import recover_incomplete_tasks
        recovered = await recover_incomplete_tasks()
        if recovered > 0:
            logger.info(
                f"Sprint 5 recovery: {recovered} незавершённых задач "
                f"помечено как failed (прервано рестартом сервера)"
            )
    except Exception as exc:
        logger.warning(f"Sprint 5 recovery failed: {exc}")

    # === Phase C.3 hotfix: восстановление "ghost"-задач (stale pending) ===
    # После фикса _TaskStub AttributeError старые pre-fix задачи оказались
    # в подвешенном состоянии: status='pending', progress=0 в БД, но в Redis
    # snapshot отсутствует (воркер не смог сохранить из-за бага). Такие
    # задачи вечно висят в UI как "Ожидание / 0%". Эта функция:
    #   1. Если Redis snapshot есть с status=done → переносит в БД как done
    #   2. Иначе если файлы есть на диске → восстанавливает как done
    #   3. Иначе помечает как failed ("прервано рестартом сервера")
    try:
        from miniapp.backend.db.repository import (
            recover_stale_pending_tasks,
            _STALE_PENDING_MINUTES,
        )
        recovered_stale = await recover_stale_pending_tasks()
        if recovered_stale > 0:
            logger.info(
                f"Phase C.3 stale-pending recovery: {recovered_stale} "
                f"ghost-задач восстановлено (stale pending > "
                f"{_STALE_PENDING_MINUTES} мин)"
            )
    except Exception as exc:
        logger.warning(f"Phase C.3 stale-pending recovery failed: {exc}")

    # === Фоновая задача: периодическая очистка старых задач ===
    # In-memory хранилище _tasks растёт без ограничений — каждая задача
    # держит мегабайты карточек ДТП, prev_cards, raw_clusters и т.д.
    # Без очистки долгоживущий сервер упадёт по OOM после ~50-100 задач.
    # Запускаем очистку каждые 2 часа, удаляем задачи старше 24 часов.
    # При наличии БД — очистка идёт и в in-memory, и в БД (см. db/repository.py).
    async def _cleanup_loop():
        while True:
            try:
                await asyncio.sleep(7200)  # 2 часа
                from miniapp.backend.services.gibdd_service import (
                    cleanup_old_tasks,
                )
                removed = await cleanup_old_tasks(max_age_hours=24)
                if removed > 0:
                    logger.info(
                        f"Cleanup: удалено {removed} старых задач "
                        f"(старше 24 часов)"
                    )
                # Этап 3: чистим протухшие карточки в dtp_cards_cache.
                # Записи с expires_at < NOW() игнорируются при SELECT,
                # но физически занимают место — удаляем.
                try:
                    from miniapp.backend.db.cards_cache import cleanup_old_cards
                    cards_removed = await cleanup_old_cards()
                    if cards_removed > 0:
                        logger.info(
                            f"Cleanup: удалено {cards_removed} протухших "
                            f"записей кэша карточек"
                        )
                except Exception as ce:
                    logger.warning(f"Cleanup cards_cache error: {ce}")

                # Этап 4: чистим протухшие очаги в clusters_cache.
                try:
                    from miniapp.backend.db.clusters_cache import (
                        cleanup_old_clusters,
                    )
                    clusters_removed = await cleanup_old_clusters()
                    if clusters_removed > 0:
                        logger.info(
                            f"Cleanup: удалено {clusters_removed} протухших "
                            f"записей кэша очагов"
                        )
                except Exception as ce:
                    logger.warning(f"Cleanup clusters_cache error: {ce}")

                # Этап 5: чистим протухшие Excel-файлы в excel_cache.
                # Записи с expires_at < NOW() игнорируются при SELECT,
                # но физически занимают место (1-2 MB каждая) — удаляем.
                try:
                    from miniapp.backend.db.excel_cache import (
                        cleanup_old_excel,
                    )
                    excel_removed = await cleanup_old_excel()
                    if excel_removed > 0:
                        logger.info(
                            f"Cleanup: удалено {excel_removed} протухших "
                            f"записей кэша Excel"
                        )
                except Exception as ce:
                    logger.warning(f"Cleanup excel_cache error: {ce}")

                # Sprint 2: чистим протухшие LLM-summary в llm_cache.
                # Записи с expires_at < NOW() игнорируются при SELECT,
                # но физически занимают место (5-10 KB каждая) — удаляем.
                try:
                    from miniapp.backend.db.llm_cache import (
                        cleanup_expired_llm_cache,
                    )
                    llm_removed = await cleanup_expired_llm_cache()
                    if llm_removed > 0:
                        logger.info(
                            f"Cleanup: удалено {llm_removed} протухших "
                            f"записей кэша LLM-summary"
                        )
                except Exception as ce:
                    logger.warning(f"Cleanup llm_cache error: {ce}")
            except asyncio.CancelledError:
                logger.info("Cleanup loop cancelled")
                break
            except Exception as exc:
                # Не роняем цикл при случайной ошибке
                logger.warning(f"Cleanup loop error: {exc}")
                await asyncio.sleep(60)

    cleanup_task = asyncio.create_task(_cleanup_loop())
    logger.info("Запущена фоновая очистка старых задач (каждые 2 часа)")

    yield

    # === Graceful shutdown ===
    logger.info("Останавливаемся...")

    # Останавливаем фоновую очистку
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass

    # === Hotfix Sprint 7: persist всех in-memory задач в БД перед закрытием пула ===
    # После получения SIGTERM/SIGINT в _tasks могут оставаться RUNNING/FETCHING/
    # PARSING задачи (если контейнер убивают во время выполнения pipeline).
    # Без этого блока они теряются навсегда — в БД остаётся последний persist'нутый
    # статус (например FETCHING), а in-memory данные (cards, raw_clusters) теряются.
    #
    # Sprint 5 recovery на startup пометит такие задачи как failed, но только если
    # они есть в БД. Если create_task не успел сделать первый persist — задача
    # полностью потеряна, и frontend будет бесконечно опрашивать /clusters с 404.
    try:
        from miniapp.backend.services.task_registry import _tasks
        from miniapp.backend.db.repository import save_task, is_db_ready

        if is_db_ready():
            # Snapshot списка task_id, чтобы не мутировать dict во время итерации
            pending_task_ids = list(_tasks.keys())
            if pending_task_ids:
                logger.info(
                    f"Shutdown: persist {len(pending_task_ids)} in-memory "
                    f"задач в БД перед закрытием пула"
                )
                persisted = 0
                for tid in pending_task_ids:
                    t = _tasks.get(tid)
                    if t is None:
                        continue
                    try:
                        await save_task(t)
                        persisted += 1
                    except Exception as exc:
                        logger.warning(
                            f"Shutdown: persist task_id={tid} failed: {exc}"
                        )
                logger.info(
                    f"Shutdown: persisted {persisted}/{len(pending_task_ids)} "
                    f"задач в БД"
                )
    except Exception as exc:
        logger.warning(f"Shutdown: in-memory tasks persist failed: {exc}")

    # Закрываем пул PostgreSQL
    try:
        await db_close_pool()
    except Exception as exc:
        logger.warning(f"Ошибка при закрытии пула БД: {exc}")

    if tg_app:
        try:
            # updater не запускали (нет start_webhook) — только stop+shutdown
            await tg_app.stop()
            await tg_app.shutdown()
            logger.info("Telegram-бот остановлен")
        except Exception as exc:
            logger.error(f"Ошибка при остановке бота: {exc}")

        # Закрываем HTTP-клиенты gibdd-bot
        try:
            from api_client import close_client
            await close_client()
        except Exception:
            pass
        try:
            from llm_analyzer import close_llm_client
            await close_llm_client()
        except Exception:
            pass

    logger.info("Сервер остановлен")


# ============================================================
# FastAPI приложение
# ============================================================
app = FastAPI(
    title="GIBDD Stat Bot + Mini App",
    description=(
        "Telegram-бот + Mini App для выгрузки и анализа данных ДТП "
        "из открытых данных ГИБДД (stat.gibdd.ru)."
    ),
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=miniapp_settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["X-Task-Id"],
)

# === Фаза 1.5: Rate limiting middleware ===
# Применяем лимит 60 req/min к /api/* (кроме exempt-эндпоинтов).
# Webhook /bot/webhook и /health* — не лимитируются.
#
# ⚠️ Sprint 4 FIX: используем PURE ASGI middleware вместо
# `app.middleware("http")(rate_limit_middleware)`.
# Starlette BaseHTTPMiddleware БУФЕРИЗУЕТ streaming responses (SSE/WebSocket),
# что ломает Sprint 4 streaming LLM — chunks доходят до клиента только
# после завершения стрима целиком. Pure ASGI middleware не трогает response
# body и пропускает SSE-стриминг без буферизации.
# Подробнее: https://github.com/encode/starlette/issues/919
from miniapp.backend.middleware.rate_limit import RateLimitASGIMiddleware
app.add_middleware(RateLimitASGIMiddleware)

# === Фаза 1.6: Prometheus metrics ===
# /metrics endpoint для скрапирования Prometheus.
# Метрики: http_requests_total, http_request_duration_seconds,
# gibdd_tasks_total, gibdd_tasks_in_progress, gibdd_cache_hits_total и др.
from miniapp.backend.middleware.metrics import setup_metrics
setup_metrics(app)

# Монтируем все роутеры Mini App под /api
app.mount("/api", miniapp_app)


# ============================================================
# Sprint 4: диагностическое логирование SSE-эндпоинтов
# Выводит при старте, зарегистрированы ли /stream маршруты,
# чтобы сразу видеть на проде, попал ли Sprint 4 в образ.
# ============================================================
try:
    _sse_routes = []
    for _route in miniapp_app.routes:
        _path = getattr(_route, "path", "")
        if "/stream" in _path and "/llm/" in _path:
            _methods = ",".join(sorted(getattr(_route, "methods", set()) or set()))
            _sse_routes.append(f"{_methods} {_path}")
    if _sse_routes:
        logger.info(f"Sprint 4: SSE endpoints registered ({len(_sse_routes)}):")
        for _r in _sse_routes:
            logger.info(f"  SSE: {_r}")
    else:
        logger.warning(
            "Sprint 4: SSE endpoints NOT registered! "
            "Проверьте miniapp/backend/routers/llm.py и sse-starlette в requirements.txt"
        )
except Exception as _e:
    logger.warning(f"Sprint 4: SSE diagnostic failed: {_e}")


# ============================================================
# Webhook для Telegram
# ============================================================
@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    """Принимает updates от Telegram и передаёт в python-telegram-bot."""
    if tg_app is None:
        raise HTTPException(
            status_code=503,
            detail="Telegram bot not initialized",
        )

    try:
        data = await request.json()
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid JSON: {exc}",
        )

    try:
        update = Update.de_json(data, tg_app.bot)
        await tg_app.process_update(update)
    except Exception as exc:
        logger.exception(f"Ошибка обработки update: {exc}")
        # Возвращаем 200, чтобы Telegram не ретраил бесконечно
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=200)

    return JSONResponse({"ok": True})


@app.get(WEBHOOK_PATH)
async def telegram_webhook_info():
    """
    Диагностический GET на /bot/webhook.
    Telegram шлёт POST, но GET нужен, чтобы в браузере проверить, что
    маршрут действительно живёт в нашем FastAPI (а не отдаётся 404 от Traefik).
    """
    return {
        "ok": True,
        "service": "gibdd-bot-miniapp",
        "webhook_path": WEBHOOK_PATH,
        "webhook_url": WEBHOOK_URL,
        "bot_initialized": tg_app is not None,
        "bothost_domain": BOTHOST_DOMAIN or "not_set",
        "port": PORT,
        "hint": (
            "Если вы видите этот JSON — FastAPI работает и маршрут /bot/webhook "
            "существует. Telegram должен слать POST сюда. Если вместо этого "
            "вы видите '404 page not found' (plain text) — запрос не доходит "
            "до контейнера: проверьте опцию 'Использовать домен' в bothost."
        ),
    }


# ============================================================
# Health check
# ============================================================
@app.get("/health")
async def health():
    """Health-check для bothost / Docker / мониторинга."""
    return {
        "status": "ok",
        "service": "gibdd-bot-miniapp",
        "version": "1.0.0",
        "telegram_bot": "running" if tg_app else "stopped",
        "bothost_domain": BOTHOST_DOMAIN or "not_set",
        "database": "ready" if db_is_ready() else "fallback (in-memory)",
    }


@app.get("/health/db")
async def health_db():
    """Детальный health-check пула PostgreSQL (для диагностики)."""
    from miniapp.backend.db.connection import health_check as db_health_check
    return await db_health_check()


@app.get("/health/db/cards")
async def health_db_cards():
    """
    Статистика кэша карточек ДТП в PostgreSQL (Этап 3).

    Возвращает:
    - configured / ready — состояние БД
    - total_entries — всего записей в dtp_cards_cache (включая протухшие)
    - valid_entries — валидных записей (expires_at > NOW())
    - total_cards_cached — суммарное количество ДТП в валидных записях
    - regions_cached — сколько регионов имеют валидные записи
    - oldest_expiry / newest_expiry — диапазон TTL
    - top_regions — топ-5 регионов по размеру кэша
    """
    from miniapp.backend.db.cards_cache import get_cache_stats
    return await get_cache_stats()


@app.get("/health/db/clusters")
async def health_db_clusters():
    """
    Статистика кэша очагов концентрации ДТП в PostgreSQL (Этап 4).

    Возвращает:
    - configured / ready — состояние БД
    - total_entries — всего записей в clusters_cache (включая протухшие)
    - valid_entries — валидных записей (expires_at > NOW())
    - total_clusters_cached — суммарное количество очагов в валидных записях
    - total_preclusters_cached — суммарное количество предочагов
    - entries_with_prev — сколько записей используют АППГ-сравнение
    - regions_cached — сколько регионов имеют валидные записи
    - oldest_expiry / newest_expiry — диапазон TTL
    - top_regions — топ-5 регионов по размеру кэша
    """
    from miniapp.backend.db.clusters_cache import get_cache_stats
    return await get_cache_stats()


@app.get("/health/db/excel")
async def health_db_excel():
    """
    Статистика кэша готовых Excel-файлов в PostgreSQL (Этап 5).

    Возвращает:
    - configured / ready — состояние БД
    - total_entries — всего записей в excel_cache (включая протухшие)
    - valid_entries — валидных записей (expires_at > NOW())
    - total_dtp_cached — суммарное количество ДТП в валидных записях
    - total_bytes / total_mb — суммарный размер байтов в кэше
    - regions_cached — сколько регионов имеют валидные записи
    - oldest_expiry / newest_expiry — диапазон TTL
    - top_regions — топ-5 регионов по размеру кэша (с разбивкой по МБ)
    """
    from miniapp.backend.db.excel_cache import get_cache_stats
    return await get_cache_stats()


@app.get("/health/redis")
async def health_redis():
    """
    Health-check Redis (Sprint 7, вариант C).

    Возвращает:
    - configured — задан ли REDIS_URL
    - use_celery — включён ли Celery-режим
    - connected — удалось ли подключиться
    - version — версия Redis сервера
    - latency_ms — задержка PING/PONG
    - db_size — количество ключей во всех БД
    - memory_used_mb / memory_max_mb — использование памяти
    - pubsub_channels — активные каналы (для LLM streaming)
    - error — текст ошибки, если что-то не так
    """
    import time
    import config

    result = {
        "configured": bool(config.REDIS_URL),
        "use_celery": config.USE_CELERY,
        "connected": False,
        "version": None,
        "latency_ms": None,
        "db_size": None,
        "memory_used_mb": None,
        "memory_max_mb": None,
        "pubsub_channels": [],
        "error": None,
    }

    if not config.REDIS_URL:
        result["error"] = "REDIS_URL not configured (in-memory mode)"
        return result

    try:
        import redis
        client = redis.from_url(config.REDIS_URL, socket_timeout=2.0, socket_connect_timeout=2.0)

        # PING с замером задержки
        start = time.perf_counter()
        pong = client.ping()
        elapsed_ms = (time.perf_counter() - start) * 1000

        if not pong:
            result["error"] = "PING returned False"
            return result

        result["connected"] = True
        result["latency_ms"] = round(elapsed_ms, 2)

        # Информация о сервере
        info = client.info()
        result["version"] = info.get("redis_version")
        result["db_size"] = client.dbsize()
        result["memory_used_mb"] = round(info.get("used_memory", 0) / 1024 / 1024, 2)
        result["memory_max_mb"] = round(info.get("maxmemory", 0) / 1024 / 1024, 2) if info.get("maxmemory") else None

        # Активные pub/sub каналы (для LLM streaming)
        channels = client.pubsub_channels(f"{config.REDIS_PUBSUB_PREFIX}:*")
        result["pubsub_channels"] = [ch.decode() if isinstance(ch, bytes) else ch for ch in channels]

    except ImportError:
        result["error"] = "redis package not installed (pip install redis)"
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"

    return result


@app.get("/health/celery")
async def health_celery():
    """
    Health-check Celery worker (Sprint 7, вариант C).

    Возвращает:
    - configured — задан ли CELERY_BROKER_URL
    - use_celery — включён ли Celery-режим
    - workers — список активных worker-инстансов
    - ping_count — сколько workers ответило на ping
    - active_tasks — количество активных задач
    - queued_tasks — задачи в очередях (по каждой очереди)
    - error — текст ошибки, если что-то не так
    """
    import config

    result = {
        "configured": bool(config.CELERY_BROKER_URL),
        "use_celery": config.USE_CELERY,
        "workers": [],
        "ping_count": 0,
        "active_tasks": 0,
        "queued_tasks": {},
        "error": None,
    }

    if not config.USE_CELERY:
        result["error"] = "USE_CELERY=false (in-memory mode)"
        return result

    if not config.CELERY_BROKER_URL:
        result["error"] = "CELERY_BROKER_URL not configured"
        return result

    try:
        from worker.celery_app import ping_worker, app

        # PING worker'ов
        ping_result = ping_worker(timeout=2.0)
        result["workers"] = ping_result["workers"]
        result["ping_count"] = ping_result["ping_count"]
        if not ping_result["ok"]:
            result["error"] = ping_result["error"]
            return result

        # Активные задачи
        inspect = app.control.inspect(timeout=2.0)
        active = inspect.active() or {}
        result["active_tasks"] = sum(len(tasks) for tasks in active.values())

        # Задачи в очередях
        import redis as redis_lib
        client = redis_lib.from_url(config.CELERY_BROKER_URL, socket_timeout=2.0)
        for queue in ["gibdd", "llm", "clusters", "exports", "celery"]:
            length = client.llen(queue)
            result["queued_tasks"][queue] = length

    except ImportError:
        result["error"] = "celery package not installed (pip install celery)"
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"

    return result

@app.get("/health/all")
async def health_all():
    """
    Агрегированный health-check всех компонентов системы.

    Возвращает 200 OK если все критичные компоненты живы.
    Возвращает 503 Service Unavailable если хотя бы один组件 упал.

    Используется как Docker HEALTHCHECK.

    Компоненты для проверки:
    - API (всегда — это сам процесс)
    - PostgreSQL (если DATABASE_URL задан)
    - Redis (если USE_CELERY=true)
    - Celery worker (если USE_CELERY=true)
    """
    import config

    checks = {}
    overall_ok = True

    # 1. API health — всегда
    checks["api"] = {"status": "ok", "telegram_bot": "running" if tg_app else "stopped"}
    if not tg_app:
        # Telegram bot не работает — это критично
        checks["api"]["status"] = "degraded"
        overall_ok = False

    # 2. PostgreSQL — если настроен
    try:
        if db_is_ready():
            checks["database"] = {"status": "ok"}
        else:
            # В single-режиме без БД — это acceptable (in-memory fallback)
            if config.DATABASE_URL:
                checks["database"] = {"status": "down", "error": "configured but not ready"}
                overall_ok = False
            else:
                checks["database"] = {"status": "not_configured"}
    except Exception as exc:
        checks["database"] = {"status": "error", "error": str(exc)}
        if config.DATABASE_URL:
            overall_ok = False

    # 3. Redis — если multi-режим
    if config.USE_CELERY:
        try:
            # Reuse health_redis logic
            redis_health = await health_redis()
            redis_ok = redis_health.get("connected", False)
            checks["redis"] = {
                "status": "ok" if redis_ok else "down",
                "latency_ms": redis_health.get("latency_ms"),
                "memory_used_mb": redis_health.get("memory_used_mb"),
                "memory_max_mb": redis_health.get("memory_max_mb"),
            }
            if not redis_ok:
                overall_ok = False

            # 4. Celery worker — если multi-режим
            celery_health = await health_celery()
            celery_ok = celery_health.get("ping_count", 0) > 0
            checks["celery"] = {
                "status": "ok" if celery_ok else "down",
                "ping_count": celery_health.get("ping_count", 0),
                "active_tasks": celery_health.get("active_tasks", 0),
                "queued_tasks": celery_health.get("queued_tasks", {}),
            }
            if not celery_ok:
                overall_ok = False
        except Exception as exc:
            checks["redis"] = {"status": "error", "error": str(exc)}
            checks["celery"] = {"status": "error", "error": str(exc)}
            overall_ok = False
    else:
        checks["redis"] = {"status": "not_configured"}
        checks["celery"] = {"status": "not_configured"}

    response_body = {
        "status": "ok" if overall_ok else "down",
        "deployment_mode": config.DEPLOYMENT_MODE if hasattr(config, "DEPLOYMENT_MODE") else "single",
        "checks": checks,
    }

    if not overall_ok:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=response_body,
        )
    return response_body

# ============================================================
# /debug/supervisor-logs — диагностика multi-режима
# ============================================================
# Stabilization P1 #4 (2026-08-20): переписан после патча A6.
#
# До A6: логи supervisord писались в /var/log/supervisor/*.log.
# Этот эндпоинт читал последние N байт каждого файла.
#
# После A6: все per-program логи идут в /dev/stdout и /dev/stderr
# (через supervisord.conf stdout_logfile=/dev/stdout), чтобы
# `docker logs <container>` их видел. Файлов в /var/log/supervisor/
# больше нет — эндпоинт стал мёртвым кодом.
#
# Текущее поведение:
#   - Без env DEBUG_LOGS_TOKEN — 403 "endpoint disabled"
#   - С токеном — возвращает информационное сообщение с командой
#     `docker logs` + (опционально) снимок /health/all.
#   - Если env DEBUG_LOGS_LEGACY=1 — пытается читать старые файлы
#     в /var/log/supervisor/ (для back-compat, если supervisord.conf
#     откачен к старой версии).
#
# Пример: GET /debug/supervisor-logs?token=secret
@app.get("/debug/supervisor-logs")
async def debug_supervisor_logs(tail: int = 20000, token: str = ""):
    import os as _os

    expected_token = _os.getenv("DEBUG_LOGS_TOKEN", "")
    if not expected_token:
        return {
            "error": "DEBUG_LOGS_TOKEN not set — debug endpoint disabled",
            "hint": "Set DEBUG_LOGS_TOKEN env var to enable",
        }
    if token != expected_token:
        return {"error": "Invalid token"}, 403

    # Ограничиваем tail сверху (для legacy-режима) — не более 200 KB на файл
    tail = max(1, min(int(tail), 200_000))

    # Режим: legacy (читать старые файлы) или modern (docker logs)
    legacy_mode = _os.getenv("DEBUG_LOGS_LEGACY", "0") == "1"

    # Шаг 1: всегда возвращаем health/all снапшот
    try:
        health_data = await health_all()
    except Exception as exc:
        health_data = {"error": f"health_all failed: {exc}"}

    # Шаг 2: если legacy — пытаемся читать старые файлы
    legacy_logs = None
    if legacy_mode:
        legacy_logs = {}
        log_dir = "/var/log/supervisor"
        names = [
            "supervisord.log",
            "api.log", "api.err.log",
            "worker.log", "worker.err.log",
            "beat.log", "beat.err.log",
            "redis.log", "redis.err.log",
        ]
        for name in names:
            path = f"{log_dir}/{name}"
            try:
                with open(path, "rb") as f:
                    try:
                        f.seek(0, 2)
                        size = f.tell()
                        read_size = min(size, tail)
                        f.seek(max(0, size - read_size))
                        raw = f.read(read_size)
                    except OSError:
                        raw = f.read()
                try:
                    text = raw.decode("utf-8", errors="replace")
                except Exception:
                    text = repr(raw)
                legacy_logs[name] = {
                    "size_bytes": len(raw),
                    "content": text,
                }
            except FileNotFoundError:
                legacy_logs[name] = None
            except Exception as exc:
                legacy_logs[name] = {"error": f"{type(exc).__name__}: {exc}"}

    # Итоговый ответ
    return {
        "tail_bytes": tail,
        "mode": "legacy" if legacy_mode else "modern",
        "modern_hint": (
            "После Stabilization A6 логи всех процессов (api, worker, beat, "
            "redis) перенаправлены в stdout/stderr контейнера. "
            "Используйте `docker logs <container>` для просмотра."
        ),
        "health": health_data if not isinstance(health_data, dict) or "error" not in health_data else health_data,
        "legacy_logs": legacy_logs,
    }


@app.get("/")
async def root():
    """Корневой endpoint с информацией о сервисе."""
    return {
        "name": "GIBDD Stat Bot + Mini App",
        "docs": "/docs",
        "health": "/health",
        "miniapp": "/app/" if FRONTEND_DIST.exists() else "frontend not built",
        "telegram_webhook": WEBHOOK_PATH,
    }


# ============================================================
# Mini App frontend (статика)
# ============================================================
if FRONTEND_DIST.exists():
    app.mount(
        "/app",
        StaticFiles(directory=str(FRONTEND_DIST), html=True),
        name="frontend",
    )
    logger.info(f"Frontend раздаётся из {FRONTEND_DIST}")

    # No-cache middleware для index.html — иначе Telegram WebView
    # кеширует HTML навсегда и не подхватывает новый JS-бандл при деплое.
    # Assets (с хешированными именами типа index-Dwtow6gx.js) кешируются
    # агрессивно — это безопасно, т.к. Vite меняет имя файла при любой правке.
    #
    # ⚠️ Sprint 4 FIX: pure ASGI middleware вместо `@app.middleware("http")`.
    # BaseHTTPMiddleware буферизует streaming responses (SSE/WebSocket).
    # Pure ASGI перехватывает send() и добавляет заголовки только для
    # http.response.start message — НЕ трогает body chunks, стриминг идёт
    # напрямую клиенту.
    class NoCacheIndexHTMLASGIMiddleware:
        """Pure ASGI: добавляет no-cache заголовки только для index.html.

        Не буферизует streaming responses (SSE/WebSocket).
        Перехватывает http.response.start message и добавляет заголовки
        ДО того, как body chunks начнут отправляться клиенту.
        """
        _TARGET_PATHS = frozenset({"/app", "/app/", "/app/index.html"})

        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope.get("type") != "http":
                await self.app(scope, receive, send)
                return

            path = scope.get("path", "")
            if path not in self._TARGET_PATHS:
                # Не наш путь — пропускаем напрямую
                await self.app(scope, receive, send)
                return

            # Перехватываем send, чтобы добавить заголовки в response.start
            async def send_wrapper(message):
                if message["type"] == "http.response.start":
                    headers = list(message.get("headers", []))
                    headers.append([b"cache-control", b"no-cache, no-store, must-revalidate"])
                    headers.append([b"pragma", b"no-cache"])
                    headers.append([b"expires", b"0"])
                    message["headers"] = headers
                await send(message)

            await self.app(scope, receive, send_wrapper)

    app.add_middleware(NoCacheIndexHTMLASGIMiddleware)
else:
    logger.warning(
        f"Frontend не собран ({FRONTEND_DIST} не существует). "
        f"Запустите `cd miniapp/frontend && npm install && npm run build`"
    )

    @app.get("/app")
    async def frontend_not_built():
        return HTMLResponse(
            "<h1>Frontend не собран</h1>"
            "<p>Выполните:</p>"
            "<pre>cd miniapp/frontend\nnpm install\nnpm run build</pre>",
            status_code=503,
        )


# ============================================================
# Точка входа для запуска напрямую (python main.py)
# ============================================================
if __name__ == "__main__":
    import uvicorn

    logger.info(f"=== GIBDD Bot + Mini App запускается на порту {PORT} ===")
    if BOTHOST_DOMAIN:
        logger.info(
            f"BOTHOST_DOMAIN: {BOTHOST_DOMAIN} | "
            f"webhook URL: {WEBHOOK_URL} | "
            f"Mini App: /app/"
        )
    else:
        logger.warning(
            "BOTHOST_DOMAIN не задан — Telegram webhook и Mini App работать не будут. "
            "Укажите BOTHOST_DOMAIN (без https://) в .env"
        )
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=PORT,
        workers=1,  # на bothost один процесс
        log_level=LOG_LEVEL.lower(),
        access_log=True,
    )
