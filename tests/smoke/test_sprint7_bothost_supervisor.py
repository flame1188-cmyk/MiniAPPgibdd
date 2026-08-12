"""
Smoke-тесты для Sprint 7 / Фаза C.1 — bothost multi-process деплой.

Проверяет:
1. docker/supervisord.conf — структура, обязательные секции, оптимизации под 2 ГБ RAM
2. docker/entrypoint.sh — переключение DEPLOYMENT_MODE=single|multi
3. Dockerfile — наличие supervisor, redis-server, entrypoint
4. env.example — секция DEPLOYMENT_MODE
5. README_DEPLOY_BOTHOST.md — инструкции multi-режима

Не запускает Docker — только статический анализ файлов.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class TestSupervisordConfig(unittest.TestCase):
    """Валидация docker/supervisord.conf."""

    def setUp(self):
        self.config_path = PROJECT_ROOT / "docker" / "supervisord.conf"
        self.assertTrue(self.config_path.exists(), f"Missing: {self.config_path}")
        self.content = self.config_path.read_text(encoding="utf-8")

    def test_has_supervisord_section(self):
        """Должна быть секция [supervisord] с nodaemon=true."""
        self.assertIn("[supervisord]", self.content)
        self.assertIn("nodaemon=true", self.content)

    def test_has_all_4_programs(self):
        """Должны быть все 4 программы: redis, api, worker, beat."""
        for program in ["redis", "api", "worker", "beat"]:
            with self.subTest(program=program):
                self.assertIn(f"[program:{program}]", self.content)

    def test_redis_optimizations_for_2gb_ram(self):
        """Redis: maxmemory 128mb (не 256mb), без AOF."""
        redis_section = self._extract_program("redis")
        self.assertIn("--maxmemory 128mb", redis_section)
        self.assertIn("--maxmemory-policy allkeys-lru", redis_section)
        self.assertIn("--appendonly no", redis_section)
        self.assertIn('--save ""', redis_section)
        # Не должно быть 256mb (старое значение из docker-compose)
        self.assertNotIn("--maxmemory 256mb", redis_section)

    def test_api_uses_env_port(self):
        """API: порт берётся из %(ENV_PORT)s, 1 worker."""
        api_section = self._extract_program("api")
        self.assertIn("%(ENV_PORT)s", api_section)
        self.assertIn("--workers 1", api_section)
        # Webhook требует 1 процесса
        self.assertNotIn("--workers 2", api_section)

    def test_worker_concurrency_4(self):
        """Worker: --concurrency=4 (по числу vCPU bothost)."""
        worker_section = self._extract_program("worker")
        self.assertIn("--concurrency=4", worker_section)

    def test_worker_max_tasks_per_child_10(self):
        """Worker: --max-tasks-per-child=10 (вместо 50, для 2 ГБ RAM)."""
        worker_section = self._extract_program("worker")
        self.assertIn("--max-tasks-per-child=10", worker_section)

    def test_worker_all_5_queues(self):
        """Worker: все 5 очередей -Q gibdd,llm,clusters,exports,celery."""
        worker_section = self._extract_program("worker")
        self.assertIn("-Q gibdd,llm,clusters,exports,celery", worker_section)

    def test_worker_time_limits(self):
        """Worker: soft_time_limit=540, time_limit=600."""
        worker_section = self._extract_program("worker")
        self.assertIn("--soft-time-limit=540", worker_section)
        self.assertIn("--time-limit=600", worker_section)

    def test_worker_prefetch_multiplier_1(self):
        """Worker: --prefetch-multiplier=1 (long-задачи не блокируют)."""
        worker_section = self._extract_program("worker")
        self.assertIn("--prefetch-multiplier=1", worker_section)

    def test_beat_schedule_path(self):
        """Beat: --schedule=/tmp/celerybeat-schedule."""
        beat_section = self._extract_program("beat")
        self.assertIn("--schedule=/tmp/celerybeat-schedule", beat_section)

    def test_programs_have_autorestart(self):
        """Все программы: autorestart=true (восстановление после падения)."""
        for program in ["redis", "api", "worker", "beat"]:
            with self.subTest(program=program):
                section = self._extract_program(program)
                self.assertIn("autorestart=true", section)
                self.assertIn("autostart=true", section)

    def test_programs_have_stopasgroup(self):
        """Все программы: stopasgroup + killasgroup (корректное завершение детей)."""
        for program in ["redis", "api", "worker", "beat"]:
            with self.subTest(program=program):
                section = self._extract_program(program)
                self.assertIn("stopasgroup=true", section)
                self.assertIn("killasgroup=true", section)

    def test_programs_have_log_files(self):
        """Все программы: отдельные stdout/stderr логи в /var/log/supervisor/."""
        for program in ["redis", "api", "worker", "beat"]:
            with self.subTest(program=program):
                section = self._extract_program(program)
                self.assertIn(f"/var/log/supervisor/{program}.log", section)
                self.assertIn(f"/var/log/supervisor/{program}.err.log", section)

    def test_redis_priority_lower_than_api(self):
        """Redis стартует раньше API (priority=10 vs 20)."""
        redis_section = self._extract_program("redis")
        api_section = self._extract_program("api")
        redis_priority = self._extract_priority(redis_section)
        api_priority = self._extract_priority(api_section)
        self.assertLess(redis_priority, api_priority,
                        "Redis must start before API")

    def test_environment_vars_set_for_api_worker_beat(self):
        """api/worker/beat: environment задаёт REDIS_URL на 127.0.0.1 (не redis:6379)."""
        for program in ["api", "worker", "beat"]:
            with self.subTest(program=program):
                section = self._extract_program(program)
                self.assertIn("REDIS_URL=\"redis://127.0.0.1:6379/0\"", section)
                self.assertIn("USE_CELERY=\"true\"", section)
                # Не должно быть redis://redis: (это для docker-compose)
                self.assertNotIn("redis://redis:", section)

    def test_worker_has_c_force_root(self):
        """Worker: C_FORCE_ROOT=true (Celery в Docker запускается от root)."""
        worker_section = self._extract_program("worker")
        self.assertIn("C_FORCE_ROOT=\"true\"", worker_section)

    def _extract_program(self, name: str) -> str:
        """Извлекает содержимое [program:name] секции."""
        pattern = rf"\[program:{name}\](.*?)(?=\n\[|\Z)"
        match = re.search(pattern, self.content, re.DOTALL)
        self.assertIsNotNone(match, f"Section [program:{name}] not found")
        return match.group(1)

    def _extract_priority(self, section: str) -> int:
        """Извлекает priority=N из секции."""
        match = re.search(r"priority=(\d+)", section)
        self.assertIsNotNone(match, "priority not found")
        return int(match.group(1))


class TestEntrypointScript(unittest.TestCase):
    """Валидация docker/entrypoint.sh."""

    def setUp(self):
        self.script_path = PROJECT_ROOT / "docker" / "entrypoint.sh"
        self.assertTrue(self.script_path.exists(), f"Missing: {self.script_path}")
        self.content = self.script_path.read_text(encoding="utf-8")

    def test_has_shebang(self):
        """Должен быть shebang #!/bin/sh (не bash, для portability)."""
        first_line = self.content.split("\n", 1)[0]
        self.assertEqual(first_line, "#!/bin/sh")

    def test_supports_single_mode(self):
        """Должна быть ветка single → exec python main.py."""
        self.assertIn("single)", self.content)
        self.assertIn("exec python main.py", self.content)

    def test_supports_multi_mode(self):
        """Должна быть ветка multi → exec supervisord."""
        self.assertIn("multi)", self.content)
        self.assertIn("exec supervisord -n -c /etc/supervisord.conf", self.content)

    def test_default_mode_is_single(self):
        """По умолчанию (если DEPLOYMENT_MODE не задан) — single."""
        # ${DEPLOYMENT_MODE:-single} — sh default syntax
        self.assertIn("${DEPLOYMENT_MODE:-single}", self.content)

    def test_creates_data_directories(self):
        """Создаёт /app/data, /data/redis, /var/log/supervisor."""
        self.assertIn("mkdir -p /app/data", self.content)
        self.assertIn("mkdir -p /data/redis", self.content)
        self.assertIn("mkdir -p /var/log/supervisor", self.content)

    def test_unknown_mode_errors_out(self):
        """Неизвестный режим → exit 1 с сообщением."""
        self.assertIn("*)", self.content)
        self.assertIn("exit 1", self.content)

    def test_uses_set_e(self):
        """set -e для fail-fast."""
        self.assertIn("set -e", self.content)


class TestDockerfile(unittest.TestCase):
    """Валидация Dockerfile."""

    def setUp(self):
        self.dockerfile_path = PROJECT_ROOT / "Dockerfile"
        self.assertTrue(self.dockerfile_path.exists(), f"Missing: {self.dockerfile_path}")
        self.content = self.dockerfile_path.read_text(encoding="utf-8")

    def test_installs_supervisor(self):
        """Должен устанавливать supervisor."""
        self.assertIn("supervisor", self.content.lower())

    def test_installs_redis_server(self):
        """Должен устанавливать redis-server."""
        self.assertIn("redis-server", self.content)

    def test_copies_supervisord_conf(self):
        """Копирует supervisord.conf в /etc/."""
        self.assertIn("COPY docker/supervisord.conf /etc/supervisord.conf", self.content)

    def test_copies_entrypoint(self):
        """Копирует entrypoint.sh и делает executable."""
        self.assertIn("COPY docker/entrypoint.sh /entrypoint.sh", self.content)
        self.assertIn("chmod +x /entrypoint.sh", self.content)

    def test_uses_entrypoint(self):
        """ENTRYPOINT указывает на /entrypoint.sh."""
        self.assertIn('ENTRYPOINT ["/entrypoint.sh"]', self.content)

    def test_creates_data_directories(self):
        """Создаёт /app/data, /data/redis, /var/log/supervisor."""
        self.assertIn("mkdir -p /app/data", self.content)
        self.assertIn("/data/redis", self.content)
        self.assertIn("/var/log/supervisor", self.content)

    def test_default_deployment_mode_single(self):
        """ENV DEPLOYMENT_MODE=single по умолчанию (backward compatible)."""
        self.assertIn("ENV DEPLOYMENT_MODE=single", self.content)

    def test_preserves_frontend_build_stage(self):
        """Stage 1: сборка frontend сохранена (multi-stage build)."""
        self.assertIn("FROM node:20-alpine AS build-frontend", self.content)
        self.assertIn("npm run build", self.content)

    def test_preserves_python_runtime_stage(self):
        """Stage 2: python:3.11-slim сохранён."""
        self.assertIn("FROM python:3.11-slim AS runtime", self.content)

    def test_healthcheck_uses_env_port(self):
        """HEALTHCHECK берёт порт из ${PORT:-8080}."""
        self.assertIn("${PORT:-8080}/health", self.content)


class TestEnvExample(unittest.TestCase):
    """Валидация env.example."""

    def setUp(self):
        self.env_path = PROJECT_ROOT / "env.example"
        self.assertTrue(self.env_path.exists(), f"Missing: {self.env_path}")
        self.content = self.env_path.read_text(encoding="utf-8")

    def test_has_deployment_mode_section(self):
        """Должна быть секция DEPLOYMENT_MODE с описанием single/multi."""
        self.assertIn("DEPLOYMENT_MODE", self.content)
        self.assertIn("single", self.content)
        self.assertIn("multi", self.content)

    def test_default_is_single(self):
        """По умолчанию DEPLOYMENT_MODE=single."""
        # Ищем строку DEPLOYMENT_MODE=single (не закомментированную)
        match = re.search(r"^DEPLOYMENT_MODE=(\w+)", self.content, re.MULTILINE)
        self.assertIsNotNone(match, "DEPLOYMENT_MODE not set")
        self.assertEqual(match.group(1), "single")

    def test_documentation_mentions_supervisord(self):
        """Документация упоминает supervisord для multi-режима."""
        self.assertIn("supervisord", self.content.lower())

    def test_documentation_mentions_4_processes(self):
        """Документация перечисляет 4 процесса multi-режима."""
        # Проверяем, что все 4 процесса упомянуты
        for process in ["redis-server", "uvicorn", "celery worker", "celery beat"]:
            with self.subTest(process=process):
                self.assertIn(process, self.content.lower())


class TestReadmeBothost(unittest.TestCase):
    """Валидация README_DEPLOY_BOTHOST.md."""

    def setUp(self):
        self.readme_path = PROJECT_ROOT / "README_DEPLOY_BOTHOST.md"
        self.assertTrue(self.readme_path.exists(), f"Missing: {self.readme_path}")
        self.content = self.readme_path.read_text(encoding="utf-8")

    def test_has_modes_section(self):
        """Должна быть секция 'Режимы деплоя'."""
        self.assertIn("Режимы деплоя", self.content)

    def test_documents_single_mode(self):
        """Описан режим single."""
        # В README: "Режим `single` (по умолчанию, текущий)"
        self.assertIn("single", self.content.lower())
        self.assertIn("по умолчанию", self.content)

    def test_documents_multi_mode(self):
        """Описан режим multi."""
        self.assertIn("режим `multi`", self.content.lower())

    def test_has_variant_b_multi_instructions(self):
        """Есть Вариант B с инструкцией multi-деплоя."""
        self.assertIn("Вариант B", self.content)
        self.assertIn("DEPLOYMENT_MODE=multi", self.content)

    def test_has_multi_mode_troubleshooting(self):
        """Есть troubleshooting для multi-режима."""
        self.assertIn("Multi-режим:", self.content)
        self.assertIn("supervisorctl", self.content)

    def test_mentions_ram_estimates(self):
        """Указаны оценки RAM для обоих режимов."""
        # single: ~300-500 MB
        self.assertIn("300-500 MB", self.content)
        # multi: ~700 MB базовое, ~1.3 GB пиковое
        self.assertIn("700 MB", self.content)
        self.assertIn("1.3 GB", self.content)

    def test_documents_cpu_requirements(self):
        """Указано требование по CPU для multi-режима."""
        self.assertIn("4 vCPU", self.content)


class TestIntegrationWithExistingInfra(unittest.TestCase):
    """Интеграционные проверки: что new-файлы не конфликтуют с существующей C.1 инфра."""

    def test_docker_compose_still_exists(self):
        """docker-compose.yml не удалён (для локальной разработки)."""
        compose_path = PROJECT_ROOT / "docker-compose.yml"
        self.assertTrue(compose_path.exists())

    def test_dockerfile_worker_still_exists(self):
        """Dockerfile.worker не удалён (используется в docker-compose)."""
        worker_dockerfile = PROJECT_ROOT / "Dockerfile.worker"
        self.assertTrue(worker_dockerfile.exists())

    def test_worker_package_still_exists(self):
        """worker/ пакет не удалён (Celery app + tasks)."""
        worker_pkg = PROJECT_ROOT / "worker"
        self.assertTrue(worker_pkg.exists())
        self.assertTrue((worker_pkg / "celery_app.py").exists())

    def test_requirements_has_celery_and_redis(self):
        """requirements.txt всё ещё содержит celery и redis."""
        req_path = PROJECT_ROOT / "requirements.txt"
        content = req_path.read_text(encoding="utf-8")
        self.assertIn("celery", content.lower())
        self.assertIn("redis", content.lower())


if __name__ == "__main__":
    unittest.main()
