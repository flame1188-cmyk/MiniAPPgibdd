"""
worker/ — Celery worker пакет (Sprint 7, вариант C).

Содержит:
- celery_app.py — конфигурация Celery (broker, backend, очереди, schedule)
- tasks/ — модули с Celery задачами:
  * gibdd_tasks.py — выгрузка карточек ДТП из stat.gibdd.ru
  * llm_tasks.py — генерация резюме и Q&A через LLM
  * clusters_tasks.py — расчёт очагов концентрации
  * exports_tasks.py — генерация Excel/HTML файлов
- redis_pubsub.py — pub/sub для streaming LLM токенов от worker к FastAPI
- task_state.py — замена OrderedDict _tasks (состояние задач в Redis)

Принцип dual-режима:
- При USE_CELERY=true и заданном REDIS_URL — задачи отправляются в Celery
- При USE_CELERY=false или без REDIS_URL — задачи выполняются in-process
  (через asyncio.create_task + semaphore, как в Sprint 6)

См. docker-compose.yml для запуска multi-process.
"""
