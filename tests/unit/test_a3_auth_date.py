"""
Дополнительные тест-кейсы для stabilization A3 — строгая проверка auth_date.

Эти тесты добавляются в существующий tests/unit/test_telegram_auth.py.
Их можно скопировать в конец файла или запустить отдельно через pytest.
"""
import hashlib
import hmac
import json
import time

import pytest
from urllib.parse import urlencode


def _build_init_data_without_auth_date(bot_token: str) -> str:
    """Строит валидную по HMAC подпись initData, но БЕЗ auth_date."""
    user_obj = {"id": 1, "first_name": "Test"}
    params = {
        "query_id": "query_1",
        "user": json.dumps(user_obj, separators=(",", ":")),
        # Намеренно НЕ добавляем auth_date
    }
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
    secret_key = hmac.new(
        b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256
    ).digest()
    hash_value = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    params["hash"] = hash_value
    return urlencode(params)


def _build_init_data_with_bad_auth_date(bot_token: str, bad_value: str) -> str:
    """Строит initData с нечисловым auth_date."""
    user_obj = {"id": 1, "first_name": "Test"}
    params = {
        "query_id": "query_1",
        "user": json.dumps(user_obj, separators=(",", ":")),
        "auth_date": bad_value,
    }
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
    secret_key = hmac.new(
        b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256
    ).digest()
    hash_value = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    params["hash"] = hash_value
    return urlencode(params)


class TestAuthDateRequired:
    """Stabilization A3: initData без валидного auth_date должно отклоняться."""

    def test_missing_auth_date_returns_none(self, test_bot_token):
        """initData без auth_date должно отклоняться (defence-in-depth)."""
        from backend.telegram_auth import _verify_init_data, settings

        init_data = _build_init_data_without_auth_date(settings.telegram_bot_token)
        result = _verify_init_data(init_data, settings.telegram_bot_token)
        assert result is None, (
            "initData без auth_date должно отклоняться, "
            "даже если HMAC подпись валидна"
        )

    def test_non_numeric_auth_date_returns_none(self, test_bot_token):
        """initData с нечисловым auth_date должно отклоняться."""
        from backend.telegram_auth import _verify_init_data, settings

        init_data = _build_init_data_with_bad_auth_date(
            settings.telegram_bot_token, "not-a-number"
        )
        result = _verify_init_data(init_data, settings.telegram_bot_token)
        assert result is None

    def test_empty_auth_date_returns_none(self, test_bot_token):
        """initData с пустым auth_date должно отклоняться."""
        from backend.telegram_auth import _verify_init_data, settings

        init_data = _build_init_data_with_bad_auth_date(
            settings.telegram_bot_token, ""
        )
        result = _verify_init_data(init_data, settings.telegram_bot_token)
        assert result is None

    def test_negative_auth_date_returns_none(self, test_bot_token):
        """initData с отрицательным auth_date должно отклоняться.

        isdigit() возвращает False для '-1', так что это попадёт под
        ту же ветку — но проверяем явно для документированности.
        """
        from backend.telegram_auth import _verify_init_data, settings

        init_data = _build_init_data_with_bad_auth_date(
            settings.telegram_bot_token, "-1"
        )
        result = _verify_init_data(init_data, settings.telegram_bot_token)
        assert result is None

    def test_valid_auth_date_still_works(self, telegram_init_data_factory, test_bot_token):
        """Регрессионный тест: валидный auth_date не должен ломаться."""
        from backend.telegram_auth import _verify_init_data, settings

        init_data = telegram_init_data_factory(
            user_id=42, bot_token=settings.telegram_bot_token
        )
        result = _verify_init_data(init_data, settings.telegram_bot_token)
        assert result is not None
        assert "user" in result
        assert "auth_date" in result

    @pytest.mark.asyncio
    async def test_missing_auth_date_in_get_current_user_raises_401(
        self, test_bot_token
    ):
        """End-to-end: через FastAPI dependency тоже должно возвращать 401."""
        from backend.telegram_auth import get_current_user, settings

        init_data = _build_init_data_without_auth_date(settings.telegram_bot_token)
        with pytest.raises(Exception) as exc_info:
            await get_current_user(tg_init_data=init_data, x_tg_init_data=None)
        # HTTPException со статусом 401
        assert hasattr(exc_info.value, "status_code")
        assert exc_info.value.status_code == 401
