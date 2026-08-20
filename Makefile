# ====================================
# Makefile — типовые операции с зависимостями
# ====================================
# Использование:
#   make compile       — сгенерировать requirements.lock из requirements.in
#   make compile-dev   — сгенерировать requirements-dev.lock
#   make sync          — синхронизировать окружение с lock (pip-sync)
#   make install       — установить из lock (pip install -r)
#   make audit         — проверить lock-файл на CVE
#   make check         — проверить, что lock не устарел
#   make upgrade       — обновить все пакеты в lock до последних совместимых версий
#   make upgrade-pkg PKG=httpx — обновить только указанный пакет в lock
#   make clean         — удалить .pyc, __pycache__, .pytest_cache
# ====================================

PYTHON ?= python3
PIP := $(PYTHON) -m pip

.PHONY: compile compile-dev sync install audit check upgrade upgrade-pkg clean

compile:
	$(PIP) install --quiet pip-tools
	pip-compile requirements.in -o requirements.lock

compile-dev:
	$(PIP) install --quiet pip-tools
	pip-compile requirements-dev.in -o requirements-dev.lock

sync: compile
	$(PIP) install pip-tools
	pip-sync requirements.lock

install:
	$(PIP) install -r requirements.lock

audit:
	$(PIP) install --quiet pip-audit
	pip-audit -r requirements.lock --strict
	@if [ -f requirements-dev.lock ]; then \
		echo "=== Auditing dev dependencies ==="; \
		pip-audit -r requirements-dev.lock --strict; \
	fi

check:
	$(PIP) install --quiet pip-tools
	@echo "=== Checking requirements.in resolves ==="
	pip-compile requirements.in --dry-run --output-file=/dev/null --quiet
	@if [ -f requirements.lock ]; then \
		echo "=== Checking requirements.lock is up-to-date ==="; \
		pip-compile requirements.in --output-file=/tmp/check.lock --quiet; \
		if ! diff <(grep -E "^[a-zA-Z]" requirements.lock | sort) <(grep -E "^[a-zA-Z]" /tmp/check.lock | sort) > /dev/null; then \
			echo "❌ requirements.lock is OUT OF DATE"; \
			echo "    Run: make compile"; \
			exit 1; \
		else \
			echo "✅ requirements.lock is up-to-date"; \
		fi \
	else \
		echo "⚠️  requirements.lock missing. Run: make compile"; \
	fi

upgrade: compile
	@echo "To upgrade: pip-compile --upgrade requirements.in -o requirements.lock"
	pip-compile --upgrade requirements.in -o requirements.lock

upgrade-pkg:
	@if [ -z "$(PKG)" ]; then echo "Usage: make upgrade-pkg PKG=httpx"; exit 1; fi
	pip-compile --upgrade-package $(PKG) requirements.in -o requirements.lock

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	rm -f /tmp/check.lock /tmp/generated.lock 2>/dev/null || true
