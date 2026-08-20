"""
Менеджер блокировок (Lock Manager).
Предоставляет контекстные менеджеры для безопасной работы с задачами.
Заменяет глобальные семафоры и примитивы синхронизации.
"""
import asyncio
import logging
from typing import Dict, Optional
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)


class LockManager:
    """
    Менеджер блокировок для задач и пользователей.
    
    Использование:
        lock_manager = LockManager()
        
        # Блокировка конкретной задачи
        async with lock_manager.task_lock(task_id):
            # критическая секция для задачи
            await process_task(task_id)
        
        # Блокировка всех задач пользователя
        async with lock_manager.user_lock(user_id):
            # критическая секция для пользователя
            await process_user_data(user_id)
    """
    
    def __init__(self, max_locks: int = 1000):
        self._task_locks: Dict[str, asyncio.Lock] = {}
        self._user_locks: Dict[int, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()  # Для создания новых блокировок
        self._max_locks = max_locks
        logger.info(f"[LockManager] Initialized: max_locks={max_locks}")

    @asynccontextmanager
    async def task_lock(self, task_id: str):
        """Контекстный менеджер для блокировки задачи."""
        lock = await self._get_or_create_task_lock(task_id)
        try:
            async with lock:
                yield
        finally:
            # Опционально: можно удалять старые блокировки
            pass

    @asynccontextmanager
    async def user_lock(self, user_id: int):
        """Контекстный менеджер для блокировки данных пользователя."""
        lock = await self._get_or_create_user_lock(user_id)
        try:
            async with lock:
                yield
        finally:
            pass

    async def _get_or_create_task_lock(self, task_id: str) -> asyncio.Lock:
        """Получение или создание блокировки для задачи."""
        async with self._global_lock:
            if task_id not in self._task_locks:
                if len(self._task_locks) >= self._max_locks:
                    await self._cleanup_old_locks('task')
                self._task_locks[task_id] = asyncio.Lock()
            return self._task_locks[task_id]

    async def _get_or_create_user_lock(self, user_id: int) -> asyncio.Lock:
        """Получение или создание блокировки для пользователя."""
        async with self._global_lock:
            if user_id not in self._user_locks:
                if len(self._user_locks) >= self._max_locks:
                    await self._cleanup_old_locks('user')
                self._user_locks[user_id] = asyncio.Lock()
            return self._user_locks[user_id]

    async def _cleanup_old_locks(self, lock_type: str):
        """Очистка старых блокировок (простая эвристика)."""
        # В простой реализации просто очищаем половину
        locks_dict = self._task_locks if lock_type == 'task' else self._user_locks
        keys_to_remove = list(locks_dict.keys())[:len(locks_dict)//2]
        for key in keys_to_remove:
            del locks_dict[key]
        logger.debug(f"[LockManager] Cleaned up {len(keys_to_remove)} {lock_type} locks")

    def get_stats(self) -> Dict[str, int]:
        """Статистика блокировок."""
        return {
            'task_locks': len(self._task_locks),
            'user_locks': len(self._user_locks),
            'max_locks': self._max_locks,
        }
