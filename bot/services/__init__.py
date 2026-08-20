"""
Модуль сервисов для бота.
Предоставляет централизованный доступ к сервисам через Dependency Injection.
"""
import os
from bot.services.state_service import TaskStateService, TaskState
from bot.services.lock_manager import LockManager


# Глобальные сервисы (инициализируются один раз при старте приложения)
# Эти переменные НЕ являются mutable state — они immutable singletons
_task_service: TaskStateService = None  # type: ignore
_lock_manager: LockManager = None  # type: ignore


def initialize_services() -> tuple[TaskStateService, LockManager]:
    """
    Инициализирует глобальные сервисы.
    Вызывается один раз при старте приложения в main.py.
    
    Returns:
        tuple[TaskStateService, LockManager]: Кортеж из двух сервисов
    """
    global _task_service, _lock_manager
    
    max_tasks = int(os.getenv('MAX_HEAVY_STATE_TASKS', '30'))
    ttl_hours = int(os.getenv('TASK_TTL_HOURS', '24'))
    max_locks = int(os.getenv('MAX_LOCKS', '1000'))
    
    _task_service = TaskStateService(
        max_tasks=max_tasks,
        ttl_hours=ttl_hours
    )
    _lock_manager = LockManager(max_locks=max_locks)
    
    return _task_service, _lock_manager


def get_task_service() -> TaskStateService:
    """
    Возвращает экземпляр TaskStateService.
    Используется для Dependency Injection.
    
    Raises:
        RuntimeError: Если сервисы ещё не инициализированы
    """
    if _task_service is None:
        raise RuntimeError(
            "TaskStateService не инициализирован. "
            "Вызовите initialize_services() перед использованием."
        )
    return _task_service


def get_lock_manager() -> LockManager:
    """
    Возвращает экземпляр LockManager.
    Используется для Dependency Injection.
    
    Raises:
        RuntimeError: Если сервисы ещё не инициализированы
    """
    if _lock_manager is None:
        raise RuntimeError(
            "LockManager не инициализирован. "
            "Вызовите initialize_services() перед использованием."
        )
    return _lock_manager


__all__ = [
    'TaskStateService',
    'TaskState',
    'LockManager',
    'initialize_services',
    'get_task_service',
    'get_lock_manager',
]