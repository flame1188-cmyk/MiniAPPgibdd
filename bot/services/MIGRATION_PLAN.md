# План рефакторинга bot/_state.py (Задача 0.4)

## Цель
Заменить глобальное изменяемое состояние (`_user_locks`, `_tasks`, `_TASKS_MEMORY`) на сервисные классы с явным жизненным циклом и Dependency Injection.

## Созданные файлы

### 1. `bot/services/state_service.py`
**Класс:** `TaskStateService`
- Заменяет глобальные словари `_TASKS_MEMORY` и `_tasks`
- Инкапсулирует логику CRUD для задач
- Thread-safe реализация через `asyncio.Lock`
- Автоматическая очистка просроченных задач
- Лимитирование количества задач (защита от OOM)

**Использование:**
```python
from bot.services import TaskStateService

# Инициализация (один раз при старте)
task_service = TaskStateService(max_tasks=30, ttl_hours=24)

# Создание задачи
task = await task_service.create_task(
    task_id="uuid-123",
    user_id=12345,
    task_type="analytics",
    payload={"region": 78}
)

# Обновление задачи
await task_service.update_task("uuid-123", status="processing", progress=50)

# Получение задачи
task = await task_service.get_task("uuid-123")

# Удаление задачи
await task_service.delete_task("uuid-123")
```

### 2. `bot/services/lock_manager.py`
**Класс:** `LockManager`
- Заменяет глобальный словарь `_user_locks`
- Предоставляет контекстные менеджеры для блокировок
- Автоматическое создание/удаление блокировок
- Защита от race conditions при обработке кнопок Telegram

**Использование:**
```python
from bot.services import LockManager

# Инициализация
lock_manager = LockManager(max_locks=1000)

# Блокировка задачи
async with lock_manager.task_lock(task_id):
    # Критическая секция - безопасная работа с задачей
    await process_task(task_id)

# Блокировка пользователя
async with lock_manager.user_lock(user_id):
    # Критическая секция - безопасная работа с данными пользователя
    await update_user_data(user_id)
```

### 3. `bot/services/__init__.py`
Централизованный экспорт сервисов для удобного импорта.

---

## Этапы интеграции

### Этап 1: Инициализация сервисов в main.py

Добавить в начало `main.py` (или в `bot/__init__.py`):

```python
from bot.services import TaskStateService, LockManager

# Глобальные сервисы (инициализируются один раз при старте)
task_service = TaskStateService(
    max_tasks=int(os.getenv('MAX_HEAVY_STATE_TASKS', '30')),
    ttl_hours=24
)
lock_manager = LockManager(max_locks=1000)

# Для доступа из других модулей через dependency injection
def get_task_service() -> TaskStateService:
    return task_service

def get_lock_manager() -> LockManager:
    return lock_manager
```

### Этап 2: Замена _user_locks

**Было:**
```python
# bot/_state.py
_user_locks: dict[int, asyncio.Lock] = {}

# bot/handlers/some_handler.py
from bot._state import _user_locks

if user_id not in _user_locks:
    _user_locks[user_id] = asyncio.Lock()

async with _user_locks[user_id]:
    # обработка запроса
    pass
```

**Стало:**
```python
# bot/handlers/some_handler.py
from bot.services import LockManager

lock_manager: LockManager  # передаётся через context или DI

async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    async with lock_manager.user_lock(user_id):
        # обработка запроса - безопасно
        pass
```

### Этап 3: Замена _tasks и _TASKS_MEMORY

**Было:**
```python
# bot/_state.py
_tasks: Dict[str, Dict] = {}
_TASKS_MEMORY: Dict[str, Any] = {}

# Создание задачи
_tasks[task_id] = {
    'task_id': task_id,
    'user_id': user_id,
    'status': 'pending',
    ...
}

# Обновление задачи
_tasks[task_id]['status'] = 'processing'
_TASKS_MEMORY[task_id] = heavy_data

# Удаление задачи
del _tasks[task_id]
del _TASKS_MEMORY[task_id]
```

**Стало:**
```python
from bot.services import TaskStateService

task_service: TaskStateService  # передаётся через DI

# Создание задачи
task = await task_service.create_task(
    task_id=task_id,
    user_id=user_id,
    task_type='analytics',
    payload={'region': region_code}
)

# Обновление задачи
await task_service.update_task(
    task_id,
    status='processing',
    progress=50,
    result=heavy_data  # вместо _TASKS_MEMORY
)

# Удаление задачи
await task_service.delete_task(task_id)
```

### Этап 4: Фоновая очистка

**Было:**
```python
# Ручная очистка в разных местах кода
```

**Стало:**
```python
# В lifespan FastAPI или фоновой задаче бота
async def cleanup_loop():
    while True:
        await asyncio.sleep(3600)  # каждый час
        await task_service.cleanup_expired()
```

---

## Преимущества нового подхода

| Аспект | Старый подход (_state.py) | Новый подход (сервисы) |
|--------|---------------------------|------------------------|
| **Потокобезопасность** | Ручные locks, риск гонок | Встроенная защита в сервисах |
| **Тестируемость** | Невозможно замокать глобалы | Легко передать mock-сервис |
| **Утечки памяти** | Нет лимитов, риск OOM | LRU eviction, лимиты |
| **Явность зависимостей** | Implicit imports через `*` | Явный DI через конструктор |
| **Масштабируемость** | Глобалы не масштабируются | Сервисы можно вынести в Redis |
| **Code Review** | Нечитаемые diff на 3000 строк | Изолированные изменения |

---

## Миграционный чеклист

- [ ] Создать `bot/services/state_service.py` ✅
- [ ] Создать `bot/services/lock_manager.py` ✅
- [ ] Создать `bot/services/__init__.py` ✅
- [ ] Обновить `main.py`: инициализация сервисов
- [ ] Обновить `bot/_state.py`: удалить `_user_locks`, `_tasks`, `_TASKS_MEMORY`
- [ ] Обновить обработчики бота: заменить прямые обращения на DI
- [ ] Добавить фоновую задачу очистки (cleanup_loop)
- [ ] Написать unit-тесты на сервисы
- [ ] Протестировать на bothost с 2-5 пользователями

---

## Оценка усилий

| Задача | Сложность | Время |
|--------|-----------|-------|
| Создание сервисов | ✅ Готово | - |
| Интеграция в main.py | Средняя | 2-3 часа |
| Рефакторинг обработчиков | Высокая | 8-12 часов |
| Тесты | Средняя | 3-4 часа |
| **Итого** | | **13-19 часов** |

---

## Риски и mitigation

| Риск | Вероятность | Mitigation |
|------|-------------|------------|
| Поломка существующей логики | Средняя | Поэтапная миграция, тесты |
| Race conditions при миграции | Низкая | Параллельный запуск старого и нового кода |
| Утечки памяти в новых сервисах | Низкая | Мониторинг RAM, лимиты |
| Долгая миграция | Высокая | Разбить на 3-4 спринта |
