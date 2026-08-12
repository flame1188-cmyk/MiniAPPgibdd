"""
miniapp.backend.core — чистая бизнес-логика, callable из Celery (Sprint 7, Фаза C.2).

Каждый модуль в core/ содержит СИНХРОННЫЕ функции, которые:
1. Не зависят от asyncio event loop (callable из Celery worker)
2. Принимают на вход ПАРАМЕТРЫ (reg_code, dat_list, cards, prev_cards),
   а не объект Task — функции pure, без side effects на task_registry
3. Возвращают результат (dict / tuple / bytes), а не мутируют состояние
4. Тестируются изолированно (см. tests/smoke/test_sprint7_phase_c2_core.py)

Модули:
- fetching.py       — fetch_cards_for_period_sync (обёртка над bot._fetch_cards_for_period)
- parsing.py        — build_excel_data_sync (обёртка над gibdd_parser.build_file1/2_data)
- analytics_core.py — build_analytics_sync (обёртка над analytics.build_full_analytics)
- exporting.py      — generate_excel_bytes_sync, generate_map_html_sync
                      (обёртки над excel_generator.generate_both_files,
                       report_generator.ReportGenerator.generate_dtp_map)
- llm_core.py       — run_llm_summary_sync, ask_llm_question_sync
                      (обёртки над llm_analyzer.get_ai_answer_stream)
- clusters_core.py  — calculate_clusters_sync
                      (обёртка над concentration_points + clusters_ops)
- pipeline_steps.py — step_fetch/step_parse/step_analytics/step_generate —
                      compositional helpers для Celery-тасков (Фаза C.3)

ВАЖНО: функции в core/ НЕ мутируют task_registry._tasks и НЕ сохраняют
состояние в Task. Они — "атомарные" операции, которые Celery-таск (C.3)
будет вызывать, а результат — сериализовать в Redis (C.4).

Backward compatibility:
- pipeline.execute_task, llm_ops.start_llm_summary, clusters_ops.start_clusters_calculation
  остаются без изменений (Фаза C.2 НЕ заменяет их, только добавляет core/ параллельно).
- В Фазе C.2.4 (опционально, после проверки пользователем) pipeline.execute_task
  будет переключён на использование core/ через asyncio.to_thread().
"""
from __future__ import annotations

# === Fetching ===
from .fetching import (
    fetch_cards_for_period_sync,
)

# === Parsing ===
from .parsing import (
    build_excel_data_sync,
)

# === Analytics ===
from .analytics_core import (
    build_analytics_sync,
)

# === Exporting (Excel + HTML map) ===
from .exporting import (
    generate_excel_bytes_sync,
    generate_map_html_sync,
)

# === LLM (Sprint 7: sync wrappers for Celery) ===
from .llm_core import (
    ask_llm_question_sync,
    run_llm_summary_sync,
)

# === Clusters (Sprint 7: sync wrapper for Celery) ===
from .clusters_core import (
    calculate_clusters_sync,
)

# === Pipeline steps (compositional, для Celery tasks в Фазе C.3) ===
from .pipeline_steps import (
    step_analytics,
    step_export,
    step_fetch,
    step_parse,
)


__all__ = [
    # Fetching
    "fetch_cards_for_period_sync",
    # Parsing
    "build_excel_data_sync",
    # Analytics
    "build_analytics_sync",
    # Exporting
    "generate_excel_bytes_sync",
    "generate_map_html_sync",
    # LLM
    "ask_llm_question_sync",
    "run_llm_summary_sync",
    # Clusters
    "calculate_clusters_sync",
    # Pipeline steps
    "step_fetch",
    "step_parse",
    "step_analytics",
    "step_export",
]
