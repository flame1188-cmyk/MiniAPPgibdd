"""worker.tasks — Celery задачи для Sprint 7 (вариант C).

Каждый модуль содержит задачи для одной очереди:
- gibdd_tasks.py    → queue "gibdd"     (pipeline выгрузки, 30-60 сек)
- llm_tasks.py      → queue "llm"       (LLM summary + Q&A, 30-60 сек)
- clusters_tasks.py → queue "clusters"  (расчёт очагов, 15-30 сек CPU)
- exports_tasks.py  → queue "exports"   (Excel/HTML генерация, 5-10 сек)
- cleanup_tasks.py  → queue "celery"    (default, для beat-задач)

Sprint 7 / Фаза C.3: все модули реализованы.
- gibdd_tasks.execute_pipeline_task — chain step_fetch → step_parse →
  step_analytics → step_export (через core/pipeline_steps)
- llm_tasks.llm_summary_task / llm_qa_task — обёртки над core/llm_core
  с pub/sub streaming в FastAPI (через worker.redis_pubsub)
- clusters_tasks.clusters_calc_task — обёртка над core/clusters_core
  с cache lookup/put через db.clusters_cache
- exports_tasks.generate_excel_task / generate_map_task — обёртки над
  core/exporting с excel_cache
- cleanup_tasks.cleanup_expired_caches / flush_stale_task_states —
  реализованы (не stubs), вызывают db.*.cleanup_old_* через asyncio.run

Все задачи:
- bind=True (self: CeleryTask — для retry/logging)
- base=CeleryTask (стандартный класс)
- acks_late=True (подтверждение после выполнения — упавший worker не теряет задачу)
- max_retries: 0 (pipeline) / 1 (LLM) / 2 (fetch_cards) — см. комментарии в модулях

Routing:
- Каждая задача явно привязана к очереди через @task(queue="...")
- Beat-задачи (cleanup) идут в default очередь "celery"
"""
