"""Initial migration - create all tables from schema.sql.

This migration creates the initial database schema based on the existing
schema.sql file. It includes:
- tasks: task metadata storage
- access_log: audit log for 152-ФЗ compliance
- dtp_cards_cache: accident cards cache
- clusters_cache: concentration points cache
- excel_cache: Excel files cache
- llm_cache: LLM summaries cache
- llm_sessions: LLM conversation sessions

Revision ID: initial
Create Date: 2025-01-20

"""
from alembic import op
import sqlalchemy as sa
from pathlib import Path


# revision identifiers, used by Alembic.
revision = 'initial'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create all tables from schema.sql."""
    
    # Get the schema.sql path - try multiple locations
    script_dir = Path(__file__).resolve().parent.parent.parent
    
    # Try project root first (where alembic.ini is)
    schema_sql_path = script_dir / 'miniapp' / 'backend' / 'db' / 'schema.sql'
    
    if not schema_sql_path.exists():
        # Fallback to relative path from workspace root
        schema_sql_path = Path('/workspace/miniapp/backend/db/schema.sql')
    
    if not schema_sql_path.exists():
        raise FileNotFoundError(
            f"schema.sql not found. Tried:\n"
            f"  1. {script_dir / 'miniapp' / 'backend' / 'db' / 'schema.sql'}\n"
            f"  2. {schema_sql_path}"
        )
    
    print(f"Loading schema from: {schema_sql_path}")
    
    with open(schema_sql_path, 'r', encoding='utf-8') as f:
        schema_sql = f.read()
    
    # Execute the schema SQL
    # PostgreSQL can handle multi-statement execution in one call
    op.execute(schema_sql)
    
    print("Initial schema created successfully from schema.sql")


def downgrade() -> None:
    """Drop all tables (careful - destructive operation)."""
    
    # Drop tables in reverse order of dependencies
    # Note: This is a destructive operation - use with caution
    
    tables_to_drop = [
        'llm_sessions',
        'llm_cache',
        'excel_cache',
        'clusters_cache',
        'dtp_cards_cache',
        'access_log',
        'tasks',
    ]
    
    for table_name in tables_to_drop:
        try:
            op.execute(f"DROP TABLE IF EXISTS {table_name} CASCADE")
            print(f"Dropped table: {table_name}")
        except Exception as e:
            print(f"Warning: Could not drop table {table_name}: {e}")
    
    # Drop the update_updated_at_column function
    op.execute("DROP FUNCTION IF EXISTS update_updated_at_column() CASCADE")
    
    print("Schema dropped successfully")
