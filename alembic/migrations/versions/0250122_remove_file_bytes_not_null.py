"""Remove NOT NULL constraint from file_bytes columns

Revision ID: 20250122_remove_file_bytes_not_null
Revises: 20250121_add_excel_file_paths
Create Date: 2025-01-22 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '20250122_remove_file_bytes_not_null'
down_revision: Union[str, None] = '20250121_add_excel_file_paths'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """
    Снимает NOT NULL ограничение с колонок file1_bytes и file2_bytes,
    так как данные теперь хранятся на диске, а не в БД.
    """
    # Для PostgreSQL используем ALTER COLUMN ... DROP NOT NULL
    op.execute('ALTER TABLE excel_cache ALTER COLUMN file1_bytes DROP NOT NULL')
    op.execute('ALTER TABLE excel_cache ALTER COLUMN file2_bytes DROP NOT NULL')
    print("✓ Removed NOT NULL constraint from file1_bytes and file2_bytes")


def downgrade() -> None:
    """
    Возвращает NOT NULL ограничение (только если все значения заполнены).
    """
    # Сначала проверим, нет ли NULL значений
    conn = op.get_bind()
    result = conn.execute(sa.text(
        "SELECT COUNT(*) FROM excel_cache WHERE file1_bytes IS NULL OR file2_bytes IS NULL"
    )).fetchone()

    if result[0] > 0:
        print(f"⚠ Cannot downgrade: {result[0]} records have NULL in file_bytes columns")
        return

    op.execute('ALTER TABLE excel_cache ALTER COLUMN file1_bytes SET NOT NULL')
    op.execute('ALTER TABLE excel_cache ALTER COLUMN file2_bytes SET NOT NULL')
    print("✓ Restored NOT NULL constraint on file1_bytes and file2_bytes")
