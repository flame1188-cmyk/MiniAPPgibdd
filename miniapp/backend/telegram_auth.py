"""
Проверка подлинности Telegram WebApp initData.

Sprint 7 fix:
- TTL увеличен с 24ч до 48ч (через TG_INITDATA_TTL_HOURS env, default=48).
  Это даёт дополнительный буфер поверх фронтенд-проверки (которая триггерит
  reload при 23ч). Telegram рекомендует 24ч, но на практике initData
  остаётся валидным дольше, и 48ч — безопасный компромисс между
  security и UX (пользователь не получает 401 при длительной сессии).
- Логирование 401 — только WARNING с причиной (без [AUTH_DIAG] INFO-шума).

Stabilization A3 fix:
- Строгая проверка auth_date: если поле отсутствует или не числовое —
  возвращаем None (defence-in-depth). Раньше TTL-проверка молча
  пропускалась при отсутствии auth_date, что позволяло обойти
  контроль срока действия подписи. HMAC всё равно проверяется, но
  по Telegram-спецификации auth_date обязателен — его отсутствие
  означает нестандартный клиент или попытку обхода.

Документация:
https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qsl

from fastapi import HTTPException, Header, Query, status

from .config import settings

logger = logging.getLogger(__name__)

# TTL для initData. По умолчанию 48 часов (с запасом поверх 24ч Telegram-лимита).
# Можно переопределить через env TG_INITDATA_TTL_HOURS.
_TTL_HOURS = int(os.getenv("TG_INITDATA_TTL_HOURS", "48"))
_TTL_SECONDS = _TTL_HOURS * 3600


@dataclass(slots=True)
class TelegramUser:
    """Распарсенный пользователь из initData."""

    id: int
    first_name: str
    last_name: str = ""
    username: str = ""
    language_code: str = "ru"
    is_premium: bool = False
    auth_date: int = 0


def _verify_init_data(init_data: str, bot_token: str) -> Optional[dict]:
    """
    Проверяет подпись initData.

    Возвращает словарь с распарсенными параметрами (без hash) или None,
    если подпись невалидна.
    """
    if not init_data:
        return None

    # Парсим как form data
    parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    hash_value = parsed.pop("hash", None)

    if not hash_value:
        return None

    # Проверяем срок действия (TG_INITDATA_TTL_HOURS, по умолчанию 48ч).
    #
    # Stabilization A3: строго требуем presence и валидность auth_date.
    # По Telegram-спецификации это обязательное поле initData. Если оно
    # отсутствует или не числовое — отклоняем подпись как невалидную
    # (defence-in-depth: даже если HMAC сошёлся, такое initData нестандартно).
    auth_date_str = parsed.get("auth_date")
    if not auth_date_str or not auth_date_str.isdigit():
        logger.warning(
            "initData rejected: missing or non-numeric auth_date "
            f"(has_auth_date={bool(auth_date_str)}, "
            f"value_type={type(auth_date_str).__name__})"
        )
        return None
    auth_date = int(auth_date_str)
    if time.time() - auth_date > _TTL_SECONDS:
        return None

    # Строим data_check_string
    data_check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(parsed.items())
    )

    # secret_key = HMAC-SHA256("WebAppData", bot_token)
    secret_key = hmac.new(
        key=b"WebAppData",
        msg=bot_token.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()

    # expected_hash = HMAC-SHA256(secret_key, data_check_string)
    expected_hash = hmac.new(
        key=secret_key,
        msg=data_check_string.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()

    # Сравнение в constant-time (защита от timing-атак)
    if not hmac.compare_digest(expected_hash, hash_value):
        return None

    return parsed


def _extract_user(parsed: dict) -> TelegramUser:
    """Извлекает TelegramUser из распарсенного initData."""
    user_raw = parsed.get("user", "")
    if not user_raw:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User field missing in initData",
        )

    import json

    try:
        user_data = json.loads(user_raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid user JSON: {exc}",
        ) from exc

    return TelegramUser(
        id=int(user_data.get("id", 0)),
        first_name=user_data.get("first_name", ""),
        last_name=user_data.get("last_name", ""),
        username=user_data.get("username", ""),
        language_code=user_data.get("language_code", "ru"),
        is_premium=bool(user_data.get("is_premium", False)),
        auth_date=int(parsed.get("auth_date", 0)),
    )


def _check_whitelist(user: TelegramUser) -> None:
    """Проверяет, есть ли пользователь в whitelist (если задан)."""
    allowed = settings.allowed_user_ids_list
    if allowed and user.id not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"User {user.id} is not in whitelist",
        )


async def get_current_user(
    tg_init_data: Optional[str] = Query(
        default=None, alias="tg_init_data", description="Telegram initData"
    ),
    x_tg_init_data: Optional[str] = Header(default=None),
) -> TelegramUser:
    """
    FastAPI dependency: извлекает и проверяет пользователя Telegram.

    initData может прийти:
    - query-параметром ?tg_init_data=... (удобно для ссылок)
    - заголовком X-Tg-Init-Data: ... (удобно для fetch из JS)
    """
    init_data = tg_init_data or x_tg_init_data

    if not init_data:
        logger.warning(
            "401 — tg_init_data не передан. "
            "Возможные причины: Mini App открыт вне Telegram, "
            "bothost-прокси стрипает X-Tg-Init-Data, "
            "или фронтенд не успел инициализировать Telegram SDK."
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="tg_init_data query parameter or X-Tg-Init-Data header required",
        )

    parsed = _verify_init_data(init_data, settings.telegram_bot_token)
    if parsed is None:
        # Логируем причину (для диагностики, но без PII)
        try:
            parsed_raw = dict(parse_qsl(init_data, keep_blank_values=True))
            has_hash = "hash" in parsed_raw
            auth_date_str = parsed_raw.get("auth_date", "")
            auth_date_int = int(auth_date_str) if auth_date_str.isdigit() else None
            age_hours = round((time.time() - auth_date_int) / 3600, 2) if auth_date_int else None
        except Exception:
            has_hash = False
            age_hours = None

        if auth_date_str and not auth_date_str.isdigit():
            reason = "invalid_auth_date (non-numeric)"
        elif not auth_date_str:
            reason = "missing_auth_date"
        elif age_hours is not None and age_hours * 3600 > _TTL_SECONDS:
            reason = f"expired (age={age_hours}h, ttl={_TTL_HOURS}h)"
        elif not has_hash:
            reason = "no_hash_in_init_data"
        elif not settings.telegram_bot_token:
            reason = "bot_token_empty_in_backend"
        else:
            reason = "bad_signature (bot_token mismatch OR initData corrupted)"

        logger.warning(
            f"401 — подпись initData невалидна. "
            f"has_hash={has_hash}, age_hours={age_hours}, "
            f"bot_token_set={bool(settings.telegram_bot_token)}, "
            f"reason={reason}"
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Telegram initData signature",
        )

    user = _extract_user(parsed)
    _check_whitelist(user)
    return user
