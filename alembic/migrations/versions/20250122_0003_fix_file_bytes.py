"""Remove NOT NULL constraint from file_bytes columns

Revision ID: 20250122_0003_fix_file_bytes
Revises: 20250121_add_excel_file_paths
Create Date: 2025-01-22 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '20250122_0003_fix_file_bytes'  # Короткий идентификатор!
down_revision: Union[str, None] = '20250121_add_excel_file_paths'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """
    Удаляет ограничение NOT NULL с колонок file1_bytes и file2_bytes,
    чтобы можно было обнулить их после переноса файлов на диск.
    """
    # Для PostgreSQL изменение типа колонки с VARCHAR NOT NULL на VARCHAR NULL
    # выполняется через ALTER COLUMN ... DROP NOT NULL
    op.alter_column('excel_cache', 'file1_bytes', existing_type=sa BYTEA(), nullable=True)
    op.alter_column('excel_cache', 'file2_bytes', existing_type=sa BYTEA(), nullable=True)


def downgrade() -> None:
    """
    Возвращает NOT NULL ограничения (если потребуется откат).
    Внимание: это может упасть, если в таблице есть NULL значения.
    """
    op.alter_column('excel_cache', 'file1_bytes', existing_type=sa BYTEA(), nullable=False)
    op.alter_column('excel_cache', 'file2_bytes', existing_type=sa BYTEA(), nullable=False)
