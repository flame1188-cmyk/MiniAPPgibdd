"""Add file_path columns to excel_cache and deprecate file_bytes columns.

This migration implements Phase 0, Task 0.3: Move Excel files from PostgreSQL
BYTEA storage to filesystem.

Changes:
- Add file1_path TEXT column
- Add file2_path TEXT column
- Keep file1_bytes/file2_bytes for backward compatibility (will be removed later)

Revision ID: 20250121_add_excel_file_paths
Create Date: 2025-01-21

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '20250121_add_excel_file_paths'
down_revision = 'initial'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add file_path columns to excel_cache table."""
    
    # Add new path columns
    op.execute("""
        ALTER TABLE excel_cache 
        ADD COLUMN IF NOT EXISTS file1_path TEXT
    """)
    
    op.execute("""
        ALTER TABLE excel_cache 
        ADD COLUMN IF NOT EXISTS file2_path TEXT
    """)
    
    print("✓ Added file1_path and file2_path columns to excel_cache")
    print("✓ Note: file1_bytes/file2_bytes columns remain for backward compatibility")
    print("✓ Run scripts/migrate_excel_to_disk.py to migrate existing data")


def downgrade() -> None:
    """Remove file_path columns (keep bytes columns)."""
    
    # Drop the new columns
    op.execute("""
        ALTER TABLE excel_cache 
        DROP COLUMN IF EXISTS file1_path
    """)
    
    op.execute("""
        ALTER TABLE excel_cache 
        DROP COLUMN IF EXISTS file2_path
    """)
    
    print("✓ Removed file1_path and file2_path columns from excel_cache")
