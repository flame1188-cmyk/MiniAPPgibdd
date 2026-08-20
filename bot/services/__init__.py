"""
Модуль сервисов для бота.
Предоставляет централизованный доступ к сервисам через Dependency Injection.
"""
from bot.services.state_service import TaskStateService, TaskState
from bot.services.lock_manager import LockManager

__all__ = [
    'TaskStateService',
    'TaskState',
    'LockManager',
]