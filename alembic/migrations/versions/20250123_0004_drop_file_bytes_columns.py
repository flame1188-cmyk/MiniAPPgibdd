"""Drop file_bytes columns after migration to disk storage

Revision ID: 20250123_0004_drop_file_bytes
Revises: 20250122_0003_fix_file_bytes
Create Date: 2025-01-23 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '20250123_0004_drop_file_bytes'
down_revision: Union[str, None] = '20250122_0003_fix_file_bytes'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """
    Удаляет колонки file1_bytes и file2_bytes из таблицы excel_cache.
    Данные уже перенесены на диск в /app/data/files.
    """
    op.drop_column('excel_cache', 'file1_bytes')
    op.drop_column('excel_cache', 'file2_bytes')


def downgrade() -> None:
    """
    Восстанавливает колонки file1_bytes и file2_bytes (для отката).
    Внимание: данные не будут восстановлены автоматически.
    """
    op.add_column('excel_cache', sa.Column('file1_bytes', sa.LargeBinary(), nullable=True))
    op.add_column('excel_cache', sa.Column('file2_bytes', sa.LargeBinary(), nullable=True))
