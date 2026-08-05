"""
Rate limiting для Mini App API (Фаза 1.5).

Защищает API от злоупотреблений:
- Баг в клиентском коде (зацикленный retry storm)
- Случайный DDoS (пользователь зажал F5)
- Скрипты, использующие API вместо Mini App UI

Стратегия:
- 60 запросов/минуту на пользователя (по telegram user_id из initData)
- 30 запросов/минуту на IP (для неавторизованных эндпоинтов типа /health)
- Exempt: /metrics, /health*, /docs, /redoc, /openapi.json — мониторинг

Реализация через slowapi (Limiter на базе in-memory sliding window).
Для multi-instance деплоя — заменить на Redis-backed limiter.
"""
from __future__ import annotations

import logging
import os
from typing import Callable

from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

# === Конфигурация ===
# 60 req/min — это 1 запрос в секунду. Достаточно для интерактивной работы
# (пользователь не нажмёт быстрее), но ловит зацикленные retry-storm'ы.
# Для long-poll эндпоинтов (?wait=25) — один poll = один запрос, 60/мин
# более чем достаточно.
DEFAULT_LIMIT = os.environ.get("RATE_LIMIT_DEFAULT", "60/minute")

# Эндпоинты, которые НЕ лимитируются (мониторинг и метаданные)
EXEMPT_PATHS = frozenset({
    "/metrics",
    "/health",
    "/health/db",
    "/health/db/cards",
    "/health/db/clusters",
    "/health/db/excel",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/favicon.ico",
    "/app/",
})


def _get_user_key(request: Request) -> str:
    """Ключ для rate limit: telegram user_id из initData (если есть),
    иначе IP-адрес.

    Возвращает строку вида "user:123456789" или "ip:1.2.3.4".
    """
    # Пытаемся достать user_id из request.state (ставится telegram_auth middleware)
    user_id = getattr(request.state, "telegram_user_id", None)
    if user_id:
        return f"user:{user_id}"

    # Fallback на IP
    return f"ip:{get_remote_address(request)}"


# Глобальный Limiter (используется в main.py через middleware)
limiter = Limiter(key_func=_get_user_key, default_limits=[DEFAULT_LIMIT])


def rate_limit_exempt(path: str) -> bool:
    """True если путь не должен лимитироваться."""
    for exempt in EXEMPT_PATHS:
        if path == exempt or path.startswith(exempt.rstrip("/") + "/"):
            return True
    return False


async def rate_limit_middleware(request: Request, call_next: Callable):
    """ASGI middleware: применяет rate limit ко всем запросам, кроме exempt.

    Использование в main.py:
        from miniapp.backend.middleware.rate_limit import rate_limit_middleware
        app.middleware("http")(rate_limit_middleware)
    """
    path = request.url.path

    # Пропускаем exempt-эндпоинты
    if rate_limit_exempt(path):
        return await call_next(request)

    # Применяем лимит
    try:
        key = _get_user_key(request)
        # Проверяем лимит через slowapi internal API
        limiter._check_request_limit(request, DEFAULT_LIMIT, key, True)
    except RateLimitExceeded as exc:
        logger.warning(
            f"Rate limit exceeded: {key} on {path} — {exc.detail}"
        )
        return JSONResponse(
            status_code=429,
            content={
                "error": "rate_limit_exceeded",
                "detail": "Слишком много запросов. Подождите минуту.",
                "retry_after_seconds": 60,
            },
            headers={
                "Retry-After": "60",
                "X-RateLimit-Limit": DEFAULT_LIMIT,
                "X-RateLimit-Reset": "60",
            },
        )
    except Exception as exc:
        # Rate limiter не должен ронять запросы — логируем и пропускаем
        logger.debug(f"Rate limit check skipped: {exc}")
        return await call_next(request)

    return await call_next(request)
