"""bot.services_container — контейнер зависимостей для бота.

Передаёт сервисы (TaskStateService, LockManager) в контекст приложения PTB,
чтобы хендлеры могли использовать их через context.bot_data.
"""
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from miniapp.backend.services.state_service import TaskStateService
    from miniapp.backend.services.lock_manager import LockManager


class BotServices:
    """Контейнер сервисов для доступа из хендлеров бота."""
    
    def __init__(
        self,
        task_state_service: 'TaskStateService',
        lock_manager: 'LockManager',
    ):
        self.task_state_service = task_state_service
        self.lock_manager = lock_manager
    
    @classmethod
    def initialize(cls, app) -> 'BotServices':
        """Инициализировать сервисы и сохранить в bot_data приложения."""
        from miniapp.backend.services.state_service import TaskStateService
        from miniapp.backend.services.lock_manager import LockManager
        
        task_state_service = TaskStateService()
        lock_manager = LockManager()
        
        services = cls(task_state_service, lock_manager)
        app.bot_data['services'] = services
        return services


def get_services(app) -> BotServices:
    """Получить сервисы из приложения PTB."""
    services = app.bot_data.get('services')
    if services is None:
        raise RuntimeError(
            "BotServices не инициализированы. "
            "Вызовите BotServices.initialize(app) перед запуском."
        )
    return services
