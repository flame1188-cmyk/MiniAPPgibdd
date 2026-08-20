"""
Адаптер для загрузки данных о ДТП напрямую из PostgreSQL (таблица dtp_cards_archive).
Используется скриптами НП БДД для ускорения работы и снижения нагрузки на API ГИБДД.
"""
import asyncio
import logging
from typing import List, Dict, Any, Optional
from datetime import datetime

logger = logging.getLogger(__name__)

# Импортируем пул соединений из основного приложения
# Предполагается, что этот файл импортируется в контексте, где db_pool уже инициализирован
try:
    from miniapp.backend.db.connection import get_db_pool
except ImportError:
    # Fallback для запуска вне контекста main.py
    get_db_pool = None


async def fetch_cards_from_db(region_code: int, month: int, year: int) -> Optional[List[Dict[str, Any]]]:
    """
    Загружает карточки ДТП из локальной БД для указанного региона и периода.
    
    Args:
        region_code: Код региона (например, 1146)
        month: Месяц (1-12)
        year: Год
        
    Returns:
        Список словарей с данными ДТП или None, если данные не найдены/ошибка.
    """
    if get_db_pool is None:
        logger.warning("DB pool not available, cannot fetch from DB")
        return None

    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            # Формируем фильтр по дате в формате JSONB пути
            # В payload->>'dat' хранится строка вида "M.YYYY"
            date_filter = f"{month}.{year}"
            
            query = """
                SELECT payload
                FROM dtp_cards_archive
                WHERE region_code = $1
                  AND payload->>'dat' = $2
            """
            
            logger.debug(f"Fetching ДТП from DB for region {region_code}, period {date_filter}")
            
            rows = await conn.fetch(query, region_code, date_filter)
            
            if not rows:
                logger.info(f"No data found in DB for region {region_code}, period {date_filter}")
                return None
            
            cards = []
            for row in rows:
                payload = row['payload']
                if isinstance(payload, dict):
                    cards.append(payload)
                elif isinstance(payload, str):
                    import json
                    try:
                        cards.append(json.loads(payload))
                    except json.JSONDecodeError:
                        continue
            
            logger.info(f"Fetched {len(cards)} cards from DB for region {region_code}, period {date_filter}")
            return cards
            
    except Exception as e:
        logger.error(f"Error fetching from DB for region {region_code}, period {date_filter}: {e}", exc_info=True)
        return None


async def check_data_availability(region_code: int, months: List[int], year: int) -> Dict[int, bool]:
    """
    Проверяет наличие данных в БД для списка месяцев.
    
    Returns:
        Словарь {month: True/False}
    """
    result = {}
    for month in months:
        cards = await fetch_cards_from_db(region_code, month, year)
        result[month] = cards is not None and len(cards) > 0
    return result
