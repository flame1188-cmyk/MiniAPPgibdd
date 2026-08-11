"""worker.tasks — Celery задачи для Sprint 7 (вариант C).

Каждый модуль содержит задачи для одной очереди:
- gibdd_tasks.py    → queue "gibdd"
- llm_tasks.py      → queue "llm"
- clusters_tasks.py → queue "clusters"
- exports_tasks.py  → queue "exports"
- cleanup_tasks.py  → queue "celery" (default, для beat-задач)

Задачи реализуются в Фазе C.3. Сейчас — заглушки для smoke-теста.
"""
