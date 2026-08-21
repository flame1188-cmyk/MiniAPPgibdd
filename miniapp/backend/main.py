"""
Mini App FastAPI приложение (монтируется в корневое приложение под /api).

НЕ запускается напрямую — используйте main.py в корне проекта.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .routers import analyze, cameras, dtp, np_bdd, parse, regions
from .version import VERSION as APP_VERSION, BUILD_TIME as APP_BUILD_TIME


app = FastAPI(
    title="GIBDD Mini App API",
    description="API для Mini App: регионы, парсинг, задачи выгрузки, очаги, точка, LLM, камеры.",
    version="0.3.0",
    docs_url="/docs",
    redoc_url="/redoc",
)


@app.get("/version")
async def get_version():
    """Текущая версия сборки backend.

    Фронтенд опрашивает этот endpoint раз в 60 сек и сравнивает со
    значением VITE_APP_VERSION, встроенным в JS-bundle. Если версии
    не совпадают — показывается баннер «Доступна новая версия,
    обновите страницу», чтобы пользователь подтянул свежий JS после
    деплоя (особенно важно для долгоживущих сессий в Telegram WebView,
    где браузерный кэш может держать старый bundle сутками).

    Не требует аутентификации — это публичный endpoint.
    """
    return {
        "version": APP_VERSION,
        "build_time": APP_BUILD_TIME,
        "service": "gibdd-miniapp",
    }

# Роутеры (могут монтироваться в родительское приложение)
app.include_router(regions.router)
app.include_router(parse.router)
app.include_router(dtp.router)
app.include_router(analyze.router)
app.include_router(cameras.router)
app.include_router(np_bdd.router)


@app.get("/miniapp/health")
async def miniapp_health():
    """Внутренний health-check Mini App."""
    return {
        "status": "ok",
        "service": "gibdd-miniapp",
        "version": "0.1.0",
    }
