"""Initial migration - mark existing schema as baseline

Revision ID: initial
Revises: 
Create Date: 2025-01-20 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'initial'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """
    Эта миграция не вносит изменений в схему БД.
    Она просто фиксирует точку отсчёта (baseline) для существующей базы данных,
    где все таблицы уже созданы через schema.sql.
    
    Если нужно создать новую БД с нуля, используйте schema.sql напрямую.
    """
    pass


def downgrade() -> None:
    """
    Downgrade не предусмотрен для baseline миграции.
    Для очистки БД используйте DROP TABLE вручную или пересоздайте БД.
    """
    pass
