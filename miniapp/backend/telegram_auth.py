"""
Проверка подлинности Telegram WebApp initData.

ДИАГНОСТИЧЕСКАЯ ВЕРСИЯ — добавлено логирование каждого 401 случая.
После определения причины вернуть оригинальный файл (telegram_auth.py.original).

Документация:
https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app

Алгоритм:
1. Получаем строку initData из query-параметра или заголовка.
2. Парсим её как URL-encoded form data.
3. Извлекаем hash, остальные параметры сортируем по ключу.
4. Строим data_check_string = "key1=value1\nkey2=value2\n...".
5. secret_key = HMAC-SHA256("WebAppData", bot_token).
6. expected_hash = HMAC-SHA256(secret_key, data_check_string) в hex.
7. Сравниваем с переданным hash.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qsl

from fastapi import HTTPException, Header, Query, status

from .config import settings

logger = logging.getLogger(__name__)


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

    # Проверяем срок действия (не старше 24 часов)
    auth_date_str = parsed.get("auth_date")
    if auth_date_str and auth_date_str.isdigit():
        auth_date = int(auth_date_str)
        # 24 часа = 86400 сек
        if time.time() - auth_date > 86400:
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

    ДИАГНОСТИЧЕСКАЯ ВЕРСИЯ — логирует каждый вызов и каждый 401.

    initData может прийти:
    - query-параметром ?tg_init_data=... (удобно для ссылок)
    - заголовком X-Tg-Init-Data: ... (удобно для fetch из JS)
    """
    # --- Диагностика: что пришло ---
    has_query_param = bool(tg_init_data)
    has_header = bool(x_tg_init_data)
    init_data = tg_init_data or x_tg_init_data

    logger.info(
        f"[AUTH_DIAG] tg_init_data(query)="
        f"{ 'present(len=' + str(len(tg_init_data)) + ')' if has_query_param else 'absent' }, "
        f"X-Tg-Init-Data(header)="
        f"{ 'present(len=' + str(len(x_tg_init_data)) + ')' if has_header else 'absent' }, "
        f"bot_token_set={ bool(settings.telegram_bot_token) }, "
        f"bot_token_len={ len(settings.telegram_bot_token) }"
    )

    if not init_data:
        logger.warning(
            "[AUTH_DIAG] 401 -> ничего не передано. Возможные причины:\n"
            "  (a) Mini App открыт вне Telegram (window.Telegram.WebApp undefined) -> "
            "getInitData() возвращает пустую строку\n"
            "  (b) bothost-прокси стрипает X-Tg-Init-Data (проверить nginx config)\n"
            "  (c) фронтенд не успел инициализировать Telegram SDK до первого запроса\n"
            "  (d) браузер блокирует custom header (CORS preflight fail)"
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="tg_init_data query parameter or X-Tg-Init-Data header required",
        )

    parsed = _verify_init_data(init_data, settings.telegram_bot_token)
    if parsed is None:
        # Детализируем причину
        try:
            parsed_raw = dict(parse_qsl(init_data, keep_blank_values=True))
            has_hash = "hash" in parsed_raw
            auth_date_str = parsed_raw.get("auth_date", "")
            auth_date_int = int(auth_date_str) if auth_date_str.isdigit() else None
            age_seconds = (
                time.time() - auth_date_int if auth_date_int else None
            )
        except Exception:
            parsed_raw = {}
            has_hash = False
            auth_date_str = ""
            age_seconds = None

        if age_seconds is not None and age_seconds > 86400:
            reason = f"expired (age={ round(age_seconds / 3600, 2) }h > 24h)"
        elif not has_hash:
            reason = "no_hash_in_init_data"
        elif not settings.telegram_bot_token:
            reason = "bot_token_empty_in_backend"
        else:
            reason = (
                "bad_signature (bot_token mismatch OR initData corrupted in transit)"
            )

        logger.warning(
            f"[AUTH_DIAG] 401 -> подпись невалидна. "
            f"has_hash={ has_hash }, "
            f"auth_date={ auth_date_str or 'N/A' }, "
            f"age_hours={ round(age_seconds / 3600, 2) if age_seconds else 'N/A' }, "
            f"bot_token_prefix="
            f"{ settings.telegram_bot_token[:10] + '...' if settings.telegram_bot_token else 'EMPTY' }, "
            f"reason={ reason }"
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Telegram initData signature",
        )

    user = _extract_user(parsed)
    _check_whitelist(user)
    logger.info(
        f"[AUTH_DIAG] OK -> user_id={ user.id }, "
        f"username={ user.username or 'N/A' }"
    )
    return user
