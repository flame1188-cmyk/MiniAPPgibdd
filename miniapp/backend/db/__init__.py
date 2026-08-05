"""
Слой работы с PostgreSQL.

Модули:
- connection: async-пул соединений (psycopg 3) с retry и health-check.
- schema.sql: CREATE TABLE IF NOT EXISTS для tasks и access_log.
- repository: TaskRepository — CRUD задач + аудит-лог,
  с transparent fallback на in-memory если БД недоступна.

См. miniapp/README.md → «Переход на production-архитектуру».
"""
