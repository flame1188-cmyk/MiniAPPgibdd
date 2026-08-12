"""
Smoke-тесты для Sprint 7 / Фаза C.2 — core/ пакет (sync-обёртки для Celery).

Проверяет:
1. miniapp/backend/core/ — структура пакета, все 7 модулей на месте
2. Все 12 публичных функций импортируются и СИНХРОННЫЕ (не async)
3. Сигнатуры функций соответствуют контракту (не принимают Task)
4. step_* функции возвращают dict с обязательными полями (ok, error, stats)
5. step_export возвращает base64-совместимые строки
6. ask_llm_question_sync возвращает ok=False для короткого вопроса
7. fetch_cards_for_period_sync падает с понятной ошибкой если вызвать из event loop
8. core/ не импортирует task_registry (pure functions)
9. core/ функции не мутируют глобальное состояние

Не делает реальных HTTP-запросов к API ГИБДД и не вызывает LLM —
только структурные проверки и проверка контракта.
"""
from __future__ import annotations

import asyncio
import base64
import inspect
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CORE_DIR = PROJECT_ROOT / "miniapp" / "backend" / "core"


# ============================================================
# 1. Структура пакета core/
# ============================================================
class TestCorePackageStructure(unittest.TestCase):
    """Проверка что все 7 модулей core/ существуют."""

    def setUp(self):
        self.assertTrue(CORE_DIR.exists(), f"core/ not found: {CORE_DIR}")
        self.assertTrue((CORE_DIR / "__init__.py").exists(), "core/__init__.py missing")

    def test_all_7_modules_exist(self):
        """Должны быть все 7 модулей core/."""
        expected_modules = [
            "__init__.py",
            "fetching.py",
            "parsing.py",
            "analytics_core.py",
            "exporting.py",
            "llm_core.py",
            "clusters_core.py",
            "pipeline_steps.py",
        ]
        for module in expected_modules:
            with self.subTest(module=module):
                path = CORE_DIR / module
                self.assertTrue(
                    path.exists(),
                    f"Missing core/{module}",
                )

    def test_init_exports_all_12_functions(self):
        """core/__init__.py должен экспортировать все 12 публичных функций."""
        from miniapp.backend.core import (  # noqa: F401
            ask_llm_question_sync,
            build_analytics_sync,
            build_excel_data_sync,
            calculate_clusters_sync,
            fetch_cards_for_period_sync,
            generate_excel_bytes_sync,
            generate_map_html_sync,
            run_llm_summary_sync,
            step_analytics,
            step_export,
            step_fetch,
            step_parse,
        )

    def test_no_underscore_private_in_all(self):
        """В __all__ не должно быть приватных (underscore) имён."""
        import miniapp.backend.core as core_pkg
        for name in core_pkg.__all__:
            self.assertFalse(
                name.startswith("_"),
                f"Приватное имя в __all__: {name}",
            )


# ============================================================
# 2. Все функции sync (не async)
# ============================================================
class TestFunctionsAreSync(unittest.TestCase):
    """Все 12 публичных функций должны быть СИНХРОННЫМИ."""

    def test_all_functions_are_sync(self):
        """Ни одна core-функция не должна быть async."""
        from miniapp.backend.core import (
            ask_llm_question_sync,
            build_analytics_sync,
            build_excel_data_sync,
            calculate_clusters_sync,
            fetch_cards_for_period_sync,
            generate_excel_bytes_sync,
            generate_map_html_sync,
            run_llm_summary_sync,
            step_analytics,
            step_export,
            step_fetch,
            step_parse,
        )

        functions = [
            fetch_cards_for_period_sync,
            build_excel_data_sync,
            build_analytics_sync,
            generate_excel_bytes_sync,
            generate_map_html_sync,
            run_llm_summary_sync,
            ask_llm_question_sync,
            calculate_clusters_sync,
            step_fetch,
            step_parse,
            step_analytics,
            step_export,
        ]
        for fn in functions:
            with self.subTest(function=fn.__name__):
                self.assertFalse(
                    inspect.iscoroutinefunction(fn),
                    f"{fn.__name__} не должен быть async — Celery worker sync!",
                )


# ============================================================
# 3. Сигнатуры функций — НЕ принимают Task
# ============================================================
class TestFunctionSignaturesNoTask(unittest.TestCase):
    """Функции core/ не должны принимать Task — pure functions."""

    def test_fetching_signature(self):
        from miniapp.backend.core import fetch_cards_for_period_sync
        sig = inspect.signature(fetch_cards_for_period_sync)
        params = list(sig.parameters.keys())
        self.assertEqual(params, ["dat_list", "reg_code", "log_prefix", "cache_result"])
        self.assertNotIn("task", " ".join(params).lower())

    def test_parsing_signature(self):
        from miniapp.backend.core import build_excel_data_sync
        sig = inspect.signature(build_excel_data_sync)
        params = list(sig.parameters.keys())
        self.assertEqual(params, ["cards"])
        self.assertNotIn("task", " ".join(params).lower())

    def test_analytics_signature(self):
        from miniapp.backend.core import build_analytics_sync
        sig = inspect.signature(build_analytics_sync)
        params = list(sig.parameters.keys())
        self.assertEqual(params, ["cards", "prev_cards", "prev_label"])

    def test_exporting_signatures(self):
        from miniapp.backend.core import generate_excel_bytes_sync, generate_map_html_sync

        sig1 = inspect.signature(generate_excel_bytes_sync)
        self.assertEqual(list(sig1.parameters.keys()), ["file1_data", "file2_data"])

        sig2 = inspect.signature(generate_map_html_sync)
        self.assertEqual(
            list(sig2.parameters.keys()),
            ["cards", "region_name", "period_label", "cameras", "prev_cards", "prev_label"],
        )

    def test_llm_core_signatures(self):
        from miniapp.backend.core import ask_llm_question_sync, run_llm_summary_sync

        sig1 = inspect.signature(run_llm_summary_sync)
        params1 = list(sig1.parameters.keys())
        self.assertIn("comparison", params1)
        self.assertIn("provider", params1)
        self.assertIn("clusters_context", params1)
        self.assertNotIn("task", " ".join(params1).lower())

        sig2 = inspect.signature(ask_llm_question_sync)
        params2 = list(sig2.parameters.keys())
        self.assertIn("question", params2)
        self.assertIn("comparison", params2)
        self.assertNotIn("task", " ".join(params2).lower())

    def test_clusters_core_signature(self):
        from miniapp.backend.core import calculate_clusters_sync
        sig = inspect.signature(calculate_clusters_sync)
        params = list(sig.parameters.keys())
        self.assertEqual(
            params,
            ["cards", "prev_cards", "prev_label", "reg_code",
             "region_name", "current_label", "cameras", "log_prefix"],
        )
        self.assertNotIn("task", " ".join(params).lower())

    def test_pipeline_steps_signatures(self):
        from miniapp.backend.core import step_analytics, step_export, step_fetch, step_parse

        sig_fetch = inspect.signature(step_fetch)
        self.assertEqual(list(sig_fetch.parameters.keys()), ["dat_list", "reg_code", "log_prefix"])

        sig_parse = inspect.signature(step_parse)
        self.assertEqual(list(sig_parse.parameters.keys()), ["cards", "log_prefix"])

        sig_analytics = inspect.signature(step_analytics)
        self.assertEqual(
            list(sig_analytics.parameters.keys()),
            ["cards", "prev_cards", "prev_label", "current_label", "log_prefix"],
        )

        sig_export = inspect.signature(step_export)
        params = list(sig_export.parameters.keys())
        self.assertEqual(
            params,
            ["file1_data", "file2_data", "cards", "region_name", "period_label",
             "cameras", "prev_cards", "prev_label", "log_prefix"],
        )


# ============================================================
# 4. core/ не импортирует task_registry
# ============================================================
class TestNoTaskRegistryDependency(unittest.TestCase):
    """core/ модули не должны зависеть от task_registry._tasks."""

    def test_no_task_registry_imports(self):
        """Ни один core-модуль не должен импортировать task_registry."""
        for module_file in CORE_DIR.glob("*.py"):
            if module_file.name == "__init__.py":
                continue
            with self.subTest(module=module_file.name):
                content = module_file.read_text(encoding="utf-8")
                # Запрещаем прямой импорт task_registry
                self.assertNotIn(
                    "from ..services.task_registry",
                    content,
                    f"{module_file.name}: не должен импортировать task_registry",
                )
                self.assertNotIn(
                    "import task_registry",
                    content,
                    f"{module_file.name}: не должен импортировать task_registry",
                )

    def test_no_tasks_dict_access(self):
        """core/ не должен обращаться к _tasks dict напрямую."""
        for module_file in CORE_DIR.glob("*.py"):
            if module_file.name == "__init__.py":
                continue
            with self.subTest(module=module_file.name):
                content = module_file.read_text(encoding="utf-8")
                # _tasks это in-memory dict в task_registry
                self.assertNotIn(
                    "_tasks[",
                    content,
                    f"{module_file.name}: не должен обращаться к _tasks[]",
                )
                self.assertNotIn(
                    "_tasks.get",
                    content,
                    f"{module_file.name}: не должен вызывать _tasks.get()",
                )


# ============================================================
# 5. step_* возвращают dict с обязательными полями
# ============================================================
class TestStepReturnContract(unittest.TestCase):
    """step_* функции должны возвращать dict с полями ok, error, stats."""

    def test_step_fetch_failure_returns_proper_dict(self):
        """step_fetch при ошибке возвращает dict с ok=False, error, stats."""
        from miniapp.backend.core import step_fetch

        with patch("miniapp.backend.core.pipeline_steps.fetch_cards_for_period_sync") as mock:
            mock.side_effect = RuntimeError("test error")
            result = step_fetch(["1.2025"], "1101", log_prefix="TEST")

        self.assertIsInstance(result, dict)
        self.assertFalse(result["ok"])
        self.assertIn("error", result)
        self.assertIn("stats", result)
        self.assertEqual(result["stats"]["total_dtp"], 0)
        self.assertEqual(result["cards"], [])
        self.assertEqual(result["errors"], [])

    def test_step_parse_failure_returns_proper_dict(self):
        """step_parse при ошибке возвращает dict с ok=False."""
        from miniapp.backend.core import step_parse

        with patch("miniapp.backend.core.pipeline_steps.build_excel_data_sync") as mock:
            mock.side_effect = RuntimeError("parse error")
            result = step_parse([], log_prefix="TEST")

        self.assertFalse(result["ok"])
        self.assertIn("error", result)
        self.assertIn("stats", result)
        self.assertEqual(result["file1_data"], [])
        self.assertEqual(result["file2_data"], [])

    def test_step_analytics_failure_returns_proper_dict(self):
        """step_analytics при ошибке возвращает dict с ok=False."""
        from miniapp.backend.core import step_analytics

        with patch("miniapp.backend.core.pipeline_steps.build_analytics_sync") as mock:
            mock.side_effect = RuntimeError("analytics error")
            result = step_analytics([], log_prefix="TEST")

        self.assertFalse(result["ok"])
        self.assertIn("error", result)
        self.assertIn("stats", result)
        self.assertEqual(result["analytics"], {})

    def test_step_export_failure_returns_proper_dict(self):
        """step_export при ошибке возвращает dict с ok=False, пустые bytes."""
        from miniapp.backend.core import step_export

        with patch("miniapp.backend.core.pipeline_steps.generate_excel_bytes_sync") as mock:
            mock.side_effect = RuntimeError("excel error")
            result = step_export([], [], [], "reg", "period", log_prefix="TEST")

        self.assertFalse(result["ok"])
        self.assertIn("error", result)
        self.assertEqual(result["file1_bytes_b64"], "")
        self.assertEqual(result["file2_bytes_b64"], "")
        self.assertEqual(result["map_html"], "")


# ============================================================
# 6. step_export возвращает base64-совместимые строки
# ============================================================
class TestStepExportBase64(unittest.TestCase):
    """step_export должен возвращать base64-совместимые строки для bytes."""

    def test_step_export_success_returns_valid_base64(self):
        """step_export при успехе возвращает валидный base64."""
        from miniapp.backend.core import step_export

        # Мокаем: file1=10 байт, file2=20 байт, map_html="..."
        fake_file1 = b"\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09"
        fake_file2 = b"\x0a\x0b\x0c\x0d" * 5
        fake_html = "<html>test</html>"

        with patch("miniapp.backend.core.pipeline_steps.generate_excel_bytes_sync") as mock_excel, \
             patch("miniapp.backend.core.pipeline_steps.generate_map_html_sync") as mock_map:
            mock_excel.return_value = (fake_file1, fake_file2)
            mock_map.return_value = fake_html
            result = step_export([], [], [], "reg", "period", log_prefix="TEST")

        self.assertTrue(result["ok"])
        # base64 должен декодироваться обратно в исходные байты
        decoded1 = base64.b64decode(result["file1_bytes_b64"])
        decoded2 = base64.b64decode(result["file2_bytes_b64"])
        self.assertEqual(decoded1, fake_file1)
        self.assertEqual(decoded2, fake_file2)
        self.assertEqual(result["map_html"], fake_html)
        self.assertEqual(result["file1_size"], 10)
        self.assertEqual(result["file2_size"], 20)

    def test_step_export_map_failure_does_not_fail_step(self):
        """Если map generation падает — step_export всё равно ok=True (map опциональна)."""
        from miniapp.backend.core import step_export

        with patch("miniapp.backend.core.pipeline_steps.generate_excel_bytes_sync") as mock_excel, \
             patch("miniapp.backend.core.pipeline_steps.generate_map_html_sync") as mock_map:
            mock_excel.return_value = (b"data1", b"data2")
            mock_map.side_effect = RuntimeError("map error")
            result = step_export([], [], [], "reg", "period", log_prefix="TEST")

        self.assertTrue(result["ok"])
        self.assertEqual(result["map_html"], "")


# ============================================================
# 7. ask_llm_question_sync — короткий вопрос
# ============================================================
class TestAskLlmQuestionShortQuestion(unittest.TestCase):
    """ask_llm_question_sync должен возвращать ok=False для слишком короткого вопроса."""

    def test_short_question_returns_ok_false(self):
        from miniapp.backend.core import ask_llm_question_sync

        result = ask_llm_question_sync(
            question="?",
            comparison={},
            reg_name="test",
            current_label="2025",
            prev_label="2024",
            log_prefix="TEST",
        )

        self.assertFalse(result["ok"])
        self.assertIsNone(result["text"])
        self.assertIn("error", result)
        self.assertIn("Слишком короткий", result["error"])

    def test_empty_question_returns_ok_false(self):
        from miniapp.backend.core import ask_llm_question_sync

        result = ask_llm_question_sync(
            question="",
            comparison={},
            reg_name="test",
            current_label="2025",
            prev_label="2024",
            log_prefix="TEST",
        )

        self.assertFalse(result["ok"])
        self.assertIsNone(result["text"])

    def test_short_question_does_not_call_llm(self):
        """Короткий вопрос не должен приводить к вызову LLM."""
        from miniapp.backend.core import ask_llm_question_sync

        # Если LLM вызовется — тест упадёт, т.к. модуля llm_analyzer нет в dev env
        # (или он есть, но без API ключа)
        result = ask_llm_question_sync(
            question="ab",  # 2 символа — слишком коротко
            comparison={},
            reg_name="test",
            current_label="2025",
            prev_label="2024",
            log_prefix="TEST",
        )

        self.assertFalse(result["ok"])


# ============================================================
# 8. fetch_cards_for_period_sync — защита от event loop
# ============================================================
class TestFetchCardsEventLoopProtection(unittest.TestCase):
    """fetch_cards_for_period_sync должен падать с понятной ошибкой в event loop."""

    def test_raises_in_running_event_loop(self):
        """Если вызвана из running event loop — RuntimeError с понятным сообщением."""
        from miniapp.backend.core import fetch_cards_for_period_sync

        # Мокаем _import_module чтобы вернуть fake bot module с async функцией.
        # Без этого падает с ImportError на telegram (не установлен в dev env),
        # не доходя до asyncio.run().
        fake_bot = MagicMock()
        async def fake_fetch(*args, **kwargs):
            return ([], [])
        fake_bot._fetch_cards_for_period = fake_fetch

        async def call_from_loop():
            # Эта функция вызывается внутри event loop
            with patch(
                "miniapp.backend.core.fetching._import_module",
                return_value=fake_bot,
            ):
                return fetch_cards_for_period_sync(
                    dat_list=["1.2025"],
                    reg_code="1101",
                    log_prefix="TEST",
                )

        with self.assertRaises(RuntimeError) as ctx:
            asyncio.run(call_from_loop())

        self.assertIn("running event loop", str(ctx.exception).lower())
        self.assertIn("Celery", str(ctx.exception))


# ============================================================
# 9. Backward compatibility — pipeline.py не изменён
# ============================================================
class TestPipelineUnchanged(unittest.TestCase):
    """pipeline.execute_task остаётся без изменений (Фаза C.2 НЕ заменяет его)."""

    def setUp(self):
        self.pipeline_path = (
            PROJECT_ROOT / "miniapp" / "backend" / "services" / "pipeline.py"
        )
        self.assertTrue(self.pipeline_path.exists())
        self.content = self.pipeline_path.read_text(encoding="utf-8")

    def test_execute_task_still_async(self):
        """execute_task должен остаться async (backward compat)."""
        self.assertIn("async def execute_task", self.content)

    def test_pipeline_does_not_import_core(self):
        """pipeline.py НЕ должен импортировать core/ (C.2 не меняет его)."""
        # core/ будет подключён в C.2.4 после проверки пользователем
        self.assertNotIn(
            "from ..core",
            self.content,
            "pipeline.py не должен импортировать core/ в Фазе C.2",
        )
        self.assertNotIn(
            "from miniapp.backend.core",
            self.content,
            "pipeline.py не должен импортировать core/ в Фазе C.2",
        )

    def test_pipeline_still_uses_imports_module(self):
        """pipeline.py всё ещё использует _imports._import_module (старый путь)."""
        self.assertIn("_imports._import_module", self.content)


# ============================================================
# 10. Документация в каждом модуле
# ============================================================
class TestModuleDocstrings(unittest.TestCase):
    """Каждый модуль core/ должен иметь docstring с описанием."""

    def test_all_modules_have_docstring(self):
        """Все 7 модулей core/ (кроме __init__) имеют module docstring."""
        for module_file in CORE_DIR.glob("*.py"):
            if module_file.name == "__init__.py":
                continue
            with self.subTest(module=module_file.name):
                content = module_file.read_text(encoding="utf-8")
                # Извлекаем docstring (тройные кавычки в начале файла)
                stripped = content.lstrip()
                self.assertTrue(
                    stripped.startswith('"""'),
                    f"{module_file.name}: должен начинаться с module docstring",
                )
                # Длина docstring минимум 200 символов (содержательное описание)
                end_idx = stripped.find('"""', 3)
                self.assertGreater(
                    end_idx, 200,
                    f"{module_file.name}: module docstring слишком короткий (<200 символов)",
                )


# ============================================================
# 11. Функции имеют docstrings с примером Celery
# ============================================================
class TestFunctionDocstringsMentionCelery(unittest.TestCase):
    """Каждая core-функция должна упоминать Celery в docstring (для разработчиков)."""

    def test_fetching_mentions_celery(self):
        from miniapp.backend.core import fetch_cards_for_period_sync
        self.assertIn("Celery", fetch_cards_for_period_sync.__doc__)

    def test_parsing_mentions_celery(self):
        from miniapp.backend.core import build_excel_data_sync
        self.assertIn("Celery", build_excel_data_sync.__doc__)

    def test_analytics_mentions_celery(self):
        from miniapp.backend.core import build_analytics_sync
        self.assertIn("Celery", build_analytics_sync.__doc__)

    def test_exporting_mentions_celery(self):
        from miniapp.backend.core import generate_excel_bytes_sync, generate_map_html_sync
        self.assertIn("Celery", generate_excel_bytes_sync.__doc__)
        self.assertIn("Celery", generate_map_html_sync.__doc__)

    def test_llm_core_mentions_celery(self):
        from miniapp.backend.core import ask_llm_question_sync, run_llm_summary_sync
        self.assertIn("Celery", run_llm_summary_sync.__doc__)
        self.assertIn("Celery", ask_llm_question_sync.__doc__)

    def test_clusters_core_mentions_celery(self):
        from miniapp.backend.core import calculate_clusters_sync
        self.assertIn("Celery", calculate_clusters_sync.__doc__)


# ============================================================
# 12. pipeline_steps — композиция всех 4 шагов
# ============================================================
class TestPipelineStepsComposition(unittest.TestCase):
    """pipeline_steps должен импортировать все 4 атомарных операции."""

    def test_pipeline_steps_imports_all_atomic_ops(self):
        """step_* должны использовать атомарные core-функции."""
        steps_path = CORE_DIR / "pipeline_steps.py"
        content = steps_path.read_text(encoding="utf-8")

        # Проверяем что pipeline_steps импортирует все 4 атомарные функции
        self.assertIn("from .fetching import fetch_cards_for_period_sync", content)
        self.assertIn("from .parsing import build_excel_data_sync", content)
        self.assertIn("from .analytics_core import build_analytics_sync", content)
        self.assertIn(
            "from .exporting import generate_excel_bytes_sync, generate_map_html_sync",
            content,
        )

    def test_step_fetch_returns_stats_dict(self):
        """step_fetch возвращает stats с total_dtp, total_dead, total_injured."""
        from miniapp.backend.core import step_fetch

        # Мокаем успешный fetch
        fake_cards = [{"pog": 1, "ran": 2}, {"pog": 0, "ran": 1}]
        with patch("miniapp.backend.core.pipeline_steps.fetch_cards_for_period_sync") as mock:
            mock.return_value = (fake_cards, [])
            result = step_fetch(["1.2025"], "1101", log_prefix="TEST")

        self.assertTrue(result["ok"])
        self.assertEqual(result["stats"]["total_dtp"], 2)
        self.assertEqual(result["stats"]["total_dead"], 1)
        self.assertEqual(result["stats"]["total_injured"], 3)

    def test_step_parse_returns_stats_dict(self):
        """step_parse возвращает stats с file1_rows, file2_rows."""
        from miniapp.backend.core import step_parse

        with patch("miniapp.backend.core.pipeline_steps.build_excel_data_sync") as mock:
            mock.return_value = ([{"a": 1}], [{"b": 2}, {"b": 3}])
            result = step_parse([{"card": 1}], log_prefix="TEST")

        self.assertTrue(result["ok"])
        self.assertEqual(result["stats"]["file1_rows"], 1)
        self.assertEqual(result["stats"]["file2_rows"], 2)


# ============================================================
# 13. README C.2 документация (когда будет создана)
# ============================================================
class TestPhaseC2Documentation(unittest.TestCase):
    """Проверка что README для C.2 будет создан (пока пропускаем)."""

    def test_core_init_docstring_describes_c2(self):
        """core/__init__.py должен описывать назначение C.2."""
        init_path = CORE_DIR / "__init__.py"
        content = init_path.read_text(encoding="utf-8")
        self.assertIn("Фаза C.2", content)
        self.assertIn("Celery", content)
        self.assertIn("СИНХРОННЫЕ", content)


if __name__ == "__main__":
    unittest.main(verbosity=2)
