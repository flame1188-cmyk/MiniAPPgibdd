"""miniapp.backend.services.lock_manager — асинхронные блокировки.

Потокобезопасная замена глобального dict _task_locks из bot/_state.py.
"""
import asyncio
from typing import Dict, Optional
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging

logger = logging.getLogger(__name__)


@dataclass
class LockInfo:
    """Информация о блокировке"""
    key: str
    acquired_at: datetime = field(default_factory=datetime.utcnow)
    owner: Optional[str] = None  # Идентификатор владельца (например, task_id или user_id)


class LockManager:
    """
    Менеджер асинхронных блокировок.
    Потокобезопасная замена глобального dict _task_locks.
    """
    
    def __init__(self, max_locks: int = 1000, ttl_hours: int = 1):
        self._locks: Dict[str, asyncio.Lock] = {}
        self._lock_info: Dict[str, LockInfo] = {}
        self._master_lock = asyncio.Lock()  # Блокировка для управления блокировками :)
        self._max_locks = max_locks
        self._ttl = timedelta(hours=ttl_hours)
        
        logger.info(f"LockManager initialized: max_locks={max_locks}, ttl={ttl_hours}h")
    
    async def acquire(
        self,
        key: str,
        timeout: float = 10.0,
        owner: Optional[str] = None,
    ) -> Optional[asyncio.Lock]:
        """
        Получить блокировку по ключу.
        
        Args:
            key: Уникальный ключ блокировки
            timeout: Таймаут ожидания в секундах
            owner: Идентификатор владельца (опционально)
            
        Returns:
            asyncio.Lock если успешно, None если таймаут
        """
        async with self._master_lock:
            if key not in self._locks:
                # Удаляем старые неиспользуемые блокировки при превышении лимита
                if len(self._locks) >= self._max_locks:
                    await self._remove_old_locks()
                
                if len(self._locks) >= self._max_locks:
                    logger.warning(f"Max locks limit reached: {self._max_locks}. Cannot create lock for {key}")
                    return None
                
                self._locks[key] = asyncio.Lock()
            
            lock = self._locks[key]
        
        # Пытаемся захватить блокировку с таймаутом
        try:
            acquired = await asyncio.wait_for(lock.acquire(), timeout=timeout)
            if acquired:
                async with self._master_lock:
                    self._lock_info[key] = LockInfo(
                        key=key,
                        acquired_at=datetime.utcnow(),
                        owner=owner,
                    )
                logger.debug(f"Lock acquired: {key} by {owner or 'unknown'}")
                return lock
        except asyncio.TimeoutError:
            logger.debug(f"Lock timeout: {key} after {timeout}s")
            return None
        
        return None
    
    async def release(self, key: str) -> None:
        """
        Освободить блокировку.
        
        Args:
            key: Ключ блокировки
        """
        async with self._master_lock:
            if key in self._locks:
                lock = self._locks[key]
                if lock.locked():
                    lock.release()
                    logger.debug(f"Lock released: {key}")
                
                # Удаляем информацию о блокировке
                if key in self._lock_info:
                    del self._lock_info[key]
    
    async def _remove_old_locks(self):
        """Удалить старые неактивные блокировки"""
        now = datetime.utcnow()
        to_remove = []
        
        for key, info in self._lock_info.items():
            if (now - info.acquired_at) > self._ttl:
                # Проверяем, не заблокирована ли ещё блокировка
                if key in self._locks:
                    lock = self._locks[key]
                    if not lock.locked():
                        to_remove.append(key)
        
        for key in to_remove:
            if key in self._locks:
                del self._locks[key]
            if key in self._lock_info:
                del self._lock_info[key]
        
        if to_remove:
            logger.info(f"Removed {len(to_remove)} old locks")
    
    async def cleanup(self):
        """Очистка перед shutdown"""
        async with self._master_lock:
            # Освобождаем все заблокированные блокировки
            for key, lock in list(self._locks.items()):
                if lock.locked():
                    try:
                        lock.release()
                    except RuntimeError:
                        pass  # Блокировка уже освобождена
            
            self._locks.clear()
            self._lock_info.clear()
        
        logger.info("LockManager cleanup complete")
    
    @property
    def active_locks_count(self) -> int:
        """Количество активных (заблокированных) блокировок"""
        return sum(1 for lock in self._locks.values() if lock.locked())
    
    @property
    def total_locks_count(self) -> int:
        """Общее количество блокировок"""
        return len(self._locks)
