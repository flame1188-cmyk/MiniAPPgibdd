"""
worker/ — Celery worker пакет (Sprint 7, вариант C).

Содержит:
- celery_app.py — конфигурация Celery (broker, backend, очереди, schedule)
- dispatcher.py — dual-mode диспетчер (Celery queue ↔ asyncio.create_task)
- task_state.py — Redis-backed task state (с in-memory fallback)
- redis_pubsub.py — pub/sub для streaming LLM токенов от worker к FastAPI
- tasks/ — модули с Celery задачами:
  * gibdd_tasks.py — execute_pipeline_task (полный pipeline выгрузки)
                     + fetch_cards_task (только выгрузка)
  * llm_tasks.py — llm_summary_task + llm_qa_task (с pub/sub streaming)
  * clusters_tasks.py — clusters_calc_task (расчёт очагов)
  * exports_tasks.py — generate_excel_task + generate_map_task
  * cleanup_tasks.py — cleanup_expired_caches + flush_stale_task_states (beat)

Принцип dual-режима (через dispatcher):
- При USE_CELERY=true и заданном REDIS_URL — задачи отправляются в Celery
  очередь, FastAPI сразу возвращает task_id, фронтенд polling'ом читает
  статус из Redis (worker.task_state).
- При USE_CELERY=false или без REDIS_URL — задачи выполняются in-process
  через asyncio.create_task + semaphore (как в Sprint 6).

См. docker-compose.yml для запуска multi-process.
"""
