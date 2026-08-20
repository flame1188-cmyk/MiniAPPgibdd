"""
Сервис управления состоянием задач (Task State Service).
Инкапсулирует логику хранения и доступа к состоянию задач,
заменяя глобальные словари _TASKS_MEMORY и _tasks.

Thread-safe и Async-safe реализация с использованием asyncio.Lock.
"""
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class TaskState:
    """Модель состояния задачи."""
    task_id: str
    user_id: int
    status: str  # 'pending', 'processing', 'completed', 'failed'
    task_type: str
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None
    progress: int = 0
    error_message: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    payload: Dict[str, Any] = field(default_factory=dict)

    def update(self, **kwargs):
        """Обновление полей задачи с автоматическим обновлением timestamp."""
        for key, value in kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)
        self.updated_at = datetime.utcnow()

    def to_dict(self) -> Dict[str, Any]:
        """Сериализация в словарь (для совместимости со старым кодом)."""
        return {
            'task_id': self.task_id,
            'user_id': self.user_id,
            'status': self.status,
            'task_type': self.task_type,
            'created_at': self.created_at.isoformat(),
            'updated_at': self.updated_at.isoformat(),
            'completed_at': self.completed_at.isoformat() if self.completed_at else None,
            'progress': self.progress,
            'error_message': self.error_message,
            'result': self.result,
            'payload': self.payload,
        }


class TaskStateService:
    """
    Сервис для управления состоянием задач в памяти.
    Заменяет глобальные словари _TASKS_MEMORY и _tasks.
    
    Использование:
        service = TaskStateService(max_tasks=100)
        await service.create_task(...)
        async with service.lock_task(task_id):
            await service.update_task(...)
    """
    
    def __init__(self, max_tasks: int = 100, ttl_hours: int = 24):
        self._tasks: Dict[str, TaskState] = {}
        self._user_tasks: Dict[int, List[str]] = {}  # user_id -> [task_ids]
        self._lock = asyncio.Lock()
        self._max_tasks = max_tasks
        self._ttl = timedelta(hours=ttl_hours)
        logger.info(f"[TaskStateService] Initialized: max_tasks={max_tasks}, ttl={ttl_hours}h")

    async def create_task(
        self,
        task_id: str,
        user_id: int,
        task_type: str,
        payload: Optional[Dict[str, Any]] = None
    ) -> TaskState:
        """Создание новой задачи."""
        async with self._lock:
            if task_id in self._tasks:
                raise ValueError(f"Task {task_id} already exists")
            
            # Проверка лимита задач
            if len(self._tasks) >= self._max_tasks:
                await self._cleanup_oldest()
            
            task = TaskState(
                task_id=task_id,
                user_id=user_id,
                status='pending',
                task_type=task_type,
                payload=payload or {}
            )
            self._tasks[task_id] = task
            
            # Индексация по пользователю
            if user_id not in self._user_tasks:
                self._user_tasks[user_id] = []
            self._user_tasks[user_id].append(task_id)
            
            logger.debug(f"[TaskStateService] Created task {task_id} for user {user_id}")
            return task

    async def get_task(self, task_id: str) -> Optional[TaskState]:
        """Получение задачи по ID."""
        async with self._lock:
            return self._tasks.get(task_id)

    async def update_task(self, task_id: str, **kwargs) -> Optional[TaskState]:
        """Обновление полей задачи."""
        async with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return None
            task.update(**kwargs)
            if kwargs.get('status') == 'completed':
                task.completed_at = datetime.utcnow()
            logger.debug(f"[TaskStateService] Updated task {task_id}: {kwargs}")
            return task

    async def delete_task(self, task_id: str) -> bool:
        """Удаление задачи."""
        async with self._lock:
            task = self._tasks.pop(task_id, None)
            if task:
                # Удаление из индекса пользователя
                if task.user_id in self._user_tasks:
                    try:
                        self._user_tasks[task.user_id].remove(task_id)
                    except ValueError:
                        pass
                logger.debug(f"[TaskStateService] Deleted task {task_id}")
                return True
            return False

    async def get_user_tasks(self, user_id: int) -> List[TaskState]:
        """Получение всех задач пользователя."""
        async with self._lock:
            task_ids = self._user_tasks.get(user_id, [])
            tasks = []
            for tid in task_ids:
                if tid in self._tasks:
                    tasks.append(self._tasks[tid])
            return tasks

    async def cleanup_expired(self) -> int:
        """Очистка старых завершенных задач."""
        async with self._lock:
            now = datetime.utcnow()
            expired = []
            for task_id, task in self._tasks.items():
                if task.status in ('completed', 'failed'):
                    if task.completed_at and (now - task.completed_at) > self._ttl:
                        expired.append(task_id)
            
            for task_id in expired:
                await self.delete_task(task_id)
            
            if expired:
                logger.info(f"[TaskStateService] Cleaned up {len(expired)} expired tasks")
            return len(expired)

    async def _cleanup_oldest(self):
        """Удаление самых старых завершенных задач при превышении лимита."""
        # Простая эвристика: удаляем до 10% самых старых завершенных задач
        completed = [
            (t.completed_at or t.updated_at, tid) 
            for tid, t in self._tasks.items() 
            if t.status in ('completed', 'failed')
        ]
        completed.sort(key=lambda x: x[0])
        
        to_delete_count = max(1, int(len(self._tasks) * 0.1))
        for _, tid in completed[:to_delete_count]:
            await self.delete_task(tid)

    async def get_stats(self) -> Dict[str, Any]:
        """Статистика сервиса."""
        async with self._lock:
            statuses = {}
            for task in self._tasks.values():
                statuses[task.status] = statuses.get(task.status, 0) + 1
            return {
                'total_tasks': len(self._tasks),
                'by_status': statuses,
                'max_tasks': self._max_tasks,
                'users_count': len(self._user_tasks),
            }
