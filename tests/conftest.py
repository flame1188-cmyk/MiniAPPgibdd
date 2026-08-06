"""
Общие фикстуры и путь импорта для всех тестов.

Добавляет /home/z/my-project/gibdd-bot в sys.path, чтобы можно было
импортировать модули проекта напрямую (без установки пакета).
"""
import sys
from pathlib import Path

# Корень проекта — где лежат analytics.py, user_request_parser.py и т.д.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Чтобы regions_builtin.py и regions_cache.py были импортируемыми
# (user_request_parser их импортирует при первом обращении).
import regions_builtin  # noqa: F401,E402  — проверка что файл на месте
