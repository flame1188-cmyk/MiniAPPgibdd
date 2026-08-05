"""
Конфигурация проекта Telegram-бота для выгрузки данных ДТП с stat.gibdd.ru.
Все ключи и настройки читаются из переменных окружения или файла .env
"""

import os
from dotenv import load_dotenv

# Загружаем переменные из файла .env (если он существует)
load_dotenv()


# ========================
# Telegram Bot
# ========================
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")

# ID пользователей, которым разрешено использовать бота (через запятую)
# Оставьте пустым, чтобы разрешить всем
ALLOWED_USER_IDS: list[int] = []
_raw_allowed = os.getenv("ALLOWED_USER_IDS", "")
if _raw_allowed:
    ALLOWED_USER_IDS = [int(uid.strip()) for uid in _raw_allowed.split(",")]

# ID администраторов для системных уведомлений (через запятую).
# Сюда будут падать алерты от monitor_cards_cache.sh и других
# внешних скриптов мониторинга (через Telegram Bot API).
# Узнать свой ID можно у @userinfobot.
# Оставьте пустым, чтобы отключить уведомления.
ADMIN_TELEGRAM_IDS: list[int] = []
_raw_admins = os.getenv("ADMIN_TELEGRAM_IDS", "")
if _raw_admins:
    ADMIN_TELEGRAM_IDS = [int(uid.strip()) for uid in _raw_admins.split(",")]


# ========================
# Сеть
# ========================
# Таймаут запросов к API stat.gibdd.ru (в секундах).
# API ГИБДД может отвечать медленно при больших выборках, ставьте 60-120.
TARGET_API_TIMEOUT: int = int(os.getenv("TARGET_API_TIMEOUT", "120"))

# Прокси (если нужен для корпоративной сети)
HTTP_PROXY: str = os.getenv("HTTP_PROXY", "")
HTTPS_PROXY: str = os.getenv("HTTPS_PROXY", "")


# ========================
# LLM — бесплатный (ZhipuAI / GLM)
# ========================
# API-ключ для ZhipuAI (GLM). Получить: https://open.bigmodel.cn
# Если не задан — кнопка "Анализ с ИИ" будет недоступна
LLM_API_KEY: str = os.getenv("LLM_API_KEY", "")

# Модель LLM (по умолчанию glm-4.7-flash — бесплатная, безлимитная, 200K контекст)
# Другие бесплатные: glm-4.5-flash, glm-4-flash-250414
LLM_MODEL: str = os.getenv("LLM_MODEL", "glm-4.7-flash")


# ========================
# LLM — платный (OpenAI-совместимый агрегатор, напр. AItunnel)
# ========================
# API-ключ для платного LLM-провайдера (AItunnel, OpenRouter и т.д.)
# Если не задан — опция "Полный (платный)" не будет показываться
LLM_PAID_API_KEY: str = os.getenv("LLM_PAID_API_KEY", "")

# URL API платного провайдера (без /chat/completions — добавляется автоматически)
# Примеры:
#   AItunnel:  https://api.aitunnel.ru/v1
#   OpenRouter: https://openrouter.ai/api/v1
LLM_PAID_API_URL: str = os.getenv("LLM_PAID_API_URL", "https://api.aitunnel.ru/v1")

# Модель платного LLM
# Примеры:
#   AItunnel:   deepseek-v4-flash, deepseek-v3, gpt-4o, claude-4-sonnet
#   OpenRouter: google/gemini-2.5-flash, deepseek/deepseek-chat
LLM_PAID_MODEL: str = os.getenv("LLM_PAID_MODEL", "deepseek-v4-flash")


# ========================
# Общие настройки LLM
# ========================
# Включать ли поиск новостей из открытых источников (Google News RSS + DuckDuckGo)
# Если "false" — нейросеть будет анализировать только данные stat.gibdd.ru
ENABLE_NEWS_SEARCH: bool = os.getenv("ENABLE_NEWS_SEARCH", "true").lower() == "true"


# ========================
# Логирование
# ========================
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")


# ========================
# PostgreSQL-кэш (Этап 3+)
# ========================
# TTL кэша карточек ДТП в секундах.
# По умолчанию 1 час (3600 сек) — баланс между актуальностью и хитами.
# Рекомендации:
#   3600     (1 час)   — production, текущий период (данные могут обновиться)
#   86400    (24 часа) — закрытые периоды прошлого месяца/года (данные стабильны)
#   604800   (7 дней)  — демо/презентации,ARCHIVE-режим
#   300      (5 мин)   — отладка/тестирование
CARDS_CACHE_TTL_SECONDS: int = int(os.getenv("CARDS_CACHE_TTL_SECONDS", "3600"))

# Будущее: TTL кэша очагов (Этап 4 — пока не используется).
# Очаги стабильнее карточек — TTL по умолчанию 6 часов.
CLUSTERS_CACHE_TTL_SECONDS: int = int(os.getenv("CLUSTERS_CACHE_TTL_SECONDS", "21600"))


# ========================
# Валидация
# ========================
def validate_config() -> list[str]:
    """Проверяет, что все обязательные настройки заданы. Возвращает список ошибок."""
    errors = []

    if not TELEGRAM_BOT_TOKEN:
        errors.append("TELEGRAM_BOT_TOKEN не задан. Получите его у @BotFather в Telegram.")

    return errors
