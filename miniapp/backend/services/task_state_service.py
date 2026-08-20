"""
Task State Service - централизованное управление состоянием задач.
Заменяет глобальные переменные из bot/_state.py
"""
import asyncio
from typing import Dict, Optional, Any
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
from enum import Enum

logger = logging.getLogger(__name__)


class TaskStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class TaskInfo:
    """Информация о задаче"""
    id: str
    user_id: int
    status: TaskStatus
    task_type: str
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    progress_percent: int = 0
    error_message: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "status": self.status.value,
            "task_type": self.task_type,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "progress_percent": self.progress_percent,
            "error_message": self.error_message,
        }


class TaskStateService:
    """
    Сервис для управления состоянием задач.
    Потокобезопасная замена глобального dict _tasks.
    """
    
    def __init__(self, max_tasks: int = 1000, ttl_hours: int = 24):
        self._tasks: Dict[str, TaskInfo] = {}
        self._lock = asyncio.Lock()
        self._max_tasks = max_tasks
        self._ttl = timedelta(hours=ttl_hours)
        self._cleanup_task: Optional[asyncio.Task] = None
        
        logger.info(f"TaskStateService initialized: max_tasks={max_tasks}, ttl={ttl_hours}h")
    
    async def create_task(
        self,
        task_id: str,
        user_id: int,
        task_type: str,
    ) -> TaskInfo:
        """Создать новую задачу"""
        async with self._lock:
            if len(self._tasks) >= self._max_tasks:
                # Удаляем старые завершённые задачи
                await self._remove_old_completed_tasks()
            
            if len(self._tasks) >= self._max_tasks:
                raise RuntimeError(f"Max tasks limit reached: {self._max_tasks}")
            
            task = TaskInfo(
                id=task_id,
                user_id=user_id,
                status=TaskStatus.PENDING,
                task_type=task_type,
            )
            self._tasks[task_id] = task
            logger.debug(f"Task created: {task_id} for user {user_id}")
            return task
    
    async def get_task(self, task_id: str) -> Optional[TaskInfo]:
        """Получить информацию о задаче"""
        async with self._lock:
            return self._tasks.get(task_id)
    
    async def update_task(
        self,
        task_id: str,
        status: Optional[TaskStatus] = None,
        progress_percent: Optional[int] = None,
        error_message: Optional[str] = None,
        result: Optional[Dict[str, Any]] = None,
    ) -> Optional[TaskInfo]:
        """Обновить задачу"""
        async with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return None
            
            if status is not None:
                task.status = status
                if status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
                    task.completed_at = datetime.utcnow()
            
            if progress_percent is not None:
                task.progress_percent = progress_percent
            
            if error_message is not None:
                task.error_message = error_message
            
            if result is not None:
                task.result = result
            
            task.updated_at = datetime.utcnow()
            logger.debug(f"Task updated: {task_id} status={task.status.value}")
            return task
    
    async def delete_task(self, task_id: str) -> bool:
        """Удалить задачу"""
        async with self._lock:
            if task_id in self._tasks:
                del self._tasks[task_id]
                logger.debug(f"Task deleted: {task_id}")
                return True
            return False
    
    async def get_user_tasks(self, user_id: int) -> list[TaskInfo]:
        """Получить все задачи пользователя"""
        async with self._lock:
            return [
                task for task in self._tasks.values()
                if task.user_id == user_id
            ]
    
    async def _remove_old_completed_tasks(self):
        """Удалить старые завершённые задачи"""
        now = datetime.utcnow()
        to_remove = []
        
        for task_id, task in self._tasks.items():
            if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
                if task.completed_at and (now - task.completed_at) > self._ttl:
                    to_remove.append(task_id)
        
        for task_id in to_remove:
            del self._tasks[task_id]
        
        if to_remove:
            logger.info(f"Removed {len(to_remove)} old completed tasks")
    
    async def cleanup(self):
        """Очистка старых задач перед shutdown"""
        async with self._lock:
            await self._remove_old_completed_tasks()
        
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        
        logger.info("TaskStateService cleanup complete")
    
    def start_background_cleanup(self, interval_minutes: int = 30):
        """Запустить фоновую очистку старых задач"""
        async def cleanup_loop():
            while True:
                await asyncio.sleep(interval_minutes * 60)
                async with self._lock:
                    await self._remove_old_completed_tasks()
        
        self._cleanup_task = asyncio.create_task(cleanup_loop())
        logger.info(f"Background cleanup started: interval={interval_minutes}m")
    
    @property
    def active_tasks_count(self) -> int:
        """Количество активных задач"""
        return sum(
            1 for task in self._tasks.values()
            if task.status in (TaskStatus.PENDING, TaskStatus.PROCESSING)
        )
    
    @property
    def total_tasks_count(self) -> int:
        """Общее количество задач"""
        return len(self._tasks)
