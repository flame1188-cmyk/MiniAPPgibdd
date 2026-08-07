# Sprint 1 — Split gibdd_service.py (2391 → 10 modules + facade)

**Дата:** 2026-08-07
**Тип:** Pure refactoring (100% backward compatible)
**Task ID:** sprint-1-gibdd-service-split

---

## Что было сделано

Файл `miniapp/backend/services/gibdd_service.py` (2391 строк, ~95 KB) разрезан
на 10 модулей по областям ответственности. Оригинальный `gibdd_service.py`
превращён в тонкий facade (~130 строк), который реэкспортирует все публичные
и приватные символы из новых модулей — поэтому **ни один импортирующий файл
(routers, main.py, db/repository.py, тесты) не требует изменений**.

## Структура пакета

```
miniapp/backend/services/
├── __init__.py            (без изменений)
├── gibdd_service.py       ← facade, ~130 строк (было 2391)
├── _imports.py            (~45 строк)  — _PROJECT_ROOT, _ensure_project_path, _import_module
├── models.py              (~130 строк) — TaskStatus, AnalysisStatus, AnalysisState, Task
├── task_registry.py       (~210 строк) — _tasks, _tasks_lock, _register_task, get_task*, list_user_tasks, _task_factory
├── query_ops.py           (~80 строк)  — parse_user_query, get_regions
├── pipeline.py            (~530 строк) — create_task, execute_task, _execute_task_impl, ensure_prev_cards, _parse_files_sync, _task_dir, _EXECUTE_SEMAPHORE
├── analytics_ops.py       (~240 строк) — _get_cross_tables, ensure_comparison, compute_point_stats
├── clusters_ops.py        (~660 строк) — start_clusters_calculation, generate_clusters_map_html, _build_clusters_map_html, _serialize_cluster, _color_for_severity, generate_clusters_excel
├── point_stats_ops.py     (~150 строк) — generate_point_stats_excel, generate_point_stats_map_html
├── llm_ops.py             (~380 строк) — start_llm_summary, _run_llm_summary_inner, ask_llm_question, get_llm_providers_status
└── cleanup.py             (~70 строк)  — cleanup_old_tasks
```

## Критический фикс: патчинг _import_module и _PROJECT_ROOT

Тесты патчат `gibdd_service._import_module`. После рефакторинга service-модули
импортируют `_import_module` через `from ._imports import _import_module` —
это создаёт **локальный binding** в каждом модуле, и патч `gibdd_service._import_module`
не распространяется на них.

**Решение:** service-модули переведены на доступ через атрибут модуля:
```python
# Было (не работает с патчами):
from ._imports import _import_module
result = _import_module(...)

# Стало (работает с патчами):
from . import _imports
result = _imports._import_module(...)
```

Скрипт `scripts/sprint1_fix_module_access.py` выполнил 44 такие замены в 8 файлах.

Тест-инфраструктура обновлена: `tests/integration/_gibdd_stubs.py` теперь патчит
и `_imports._import_module` (параллельно с `gibdd_service._import_module`).
5 тестовых файлов получили аналогичные параллельные патчи для `_PROJECT_ROOT`.

## Результаты тестирования

- **Smoke-тест** (`scripts/smoke_sprint1_facade.py`): **30/30 PASSED ✓**
  - Публичные импорты (create_task, get_task, list_user_tasks, ...)
  - Приватные импорты (_tasks, _task_factory, _task_dir, _serialize_cluster, _color_for_severity, ...)
  - Типы объектов (OrderedDict, threading.Lock, asyncio.Semaphore)
  - Identity _tasks (один и тот же объект во всех модулях)
- **FastAPI app init**: OK (38 routes)
- **Unit + integration tests**: **414 passed, 1 failed, 30 skipped**
  - ДО рефакторинга (stashed): 43 failed, 372 passed
  - ПОСЛЕ: 1 failed, 414 passed → рефакторинг + фикс тест-стабов **ИСПРАВИЛИ 42 ранее падавших теста**
  - Единственный remaining failure (`TestLruEviction::test_eviction_when_limit_exceeded`) —
    pre-existing, не связан с рефакторингом

## Что НЕ изменилось

- `miniapp/backend/routers/dtp.py` — без изменений
- `miniapp/backend/routers/analyze.py` — без изменений
- `miniapp/backend/routers/parse.py` — без изменений
- `miniapp/backend/routers/regions.py` — без изменений
- `miniapp/backend/main.py` — без изменений
- `miniapp/backend/db/repository.py` — без изменений
- Поведение runtime — идентично оригиналу (pure refactoring)

## Установка

### Вариант A: полная замена (рекомендуется)

```bash
# 1. Бэкап
cp -r miniapp/backend/services miniapp/backend/services.bak
cp -r tests/integration tests/integration.bak
cp tests/unit/test_gibdd_service.py tests/unit/test_gibdd_service.py.bak

# 2. Распаковать архив
unzip sprint1-gibdd-service-split.zip -d /tmp/sprint1

# 3. Скопировать файлы
cp /tmp/sprint1/miniapp/backend/services/*.py miniapp/backend/services/
cp /tmp/sprint1/tests/integration/*.py tests/integration/
cp /tmp/sprint1/tests/unit/test_gibdd_service.py tests/unit/
cp /tmp/sprint1/scripts/*.py scripts/

# 4. Проверить smoke-тест
python scripts/smoke_sprint1_facade.py

# 5. Запустить тесты
pytest tests/ -x --tb=short

# 6. Перезапустить MiniApp
# На bothost: deploy через git push или Docker rebuild
```

### Вариант B: только services (без тестов)

Если тесты в проде не запускаются — достаточно скопировать только `miniapp/backend/services/*.py`.

## Откат

```bash
cp -r miniapp/backend/services.bak/* miniapp/backend/services/
# ИЛИ если нет бэкапа — восстановить gibdd_service.py из git
git checkout HEAD -- miniapp/backend/services/gibdd_service.py
rm miniapp/backend/services/{_imports,models,task_registry,query_ops,pipeline,analytics_ops,clusters_ops,point_stats_ops,llm_ops,cleanup}.py
```

## Что дальше

- **Sprint 2 (Phase 4-2):** Split `routers/analyze.py` (759 строк, 12 endpoints) →
  `routers/clusters.py`, `routers/point_stats.py`, `routers/llm.py`
- **Sprint 3 (Phase 4-3):** Split крупных `.tsx` (NpBddView 1088, AnalyticsView 887, ClustersView 646)
- **Variant A improvements:** Task recovery on startup, LLM_SEMAPHORE, LRU on _user_locks, SSE for progress, streaming LLM
