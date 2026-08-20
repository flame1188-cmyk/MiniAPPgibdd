"""Initial migration - create all tables from schema.sql

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
    # === tasks ===
    op.create_table('tasks',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('task_type', sa.String(), nullable=False),
        sa.Column('region_code', sa.Integer(), nullable=True),
        sa.Column('period', sa.String(), nullable=True),
        sa.Column('settlement', sa.String(), nullable=True),
        sa.Column('year', sa.Integer(), nullable=True),
        sa.Column('filters', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('progress_percent', sa.Integer(), nullable=True),
        sa.Column('files', sa.JSON(), nullable=True),
        sa.Column('analytics', sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_tasks_user_id'), 'tasks', ['user_id'], unique=False)
    op.create_index(op.f('ix_tasks_created_at'), 'tasks', ['created_at'], unique=False)

    # === access_log ===
    op.create_table('access_log',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column('timestamp', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('action', sa.String(), nullable=False),
        sa.Column('task_id', sa.String(), nullable=True),
        sa.Column('details', sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_access_log_timestamp'), 'access_log', ['timestamp'], unique=False)
    op.create_index(op.f('ix_access_log_user_id'), 'access_log', ['user_id'], unique=False)

    # === llm_sessions ===
    op.create_table('llm_sessions',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('task_id', sa.String(), nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('message_count', sa.Integer(), server_default='0', nullable=False),
        sa.Column('last_message_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_llm_sessions_task_id'), 'llm_sessions', ['task_id'], unique=False)

    # === llm_messages ===
    op.create_table('llm_messages',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column('session_id', sa.String(), nullable=False),
        sa.Column('role', sa.String(), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('timestamp', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('tokens_used', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['session_id'], ['llm_sessions.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_llm_messages_session_id'), 'llm_messages', ['session_id'], unique=False)

    # === cards_cache ===
    op.create_table('cards_cache',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column('region_code', sa.Integer(), nullable=False),
        sa.Column('dat_hash', sa.String(), nullable=False),
        sa.Column('payload', sa.JSONB(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_cards_cache_region_code'), 'cards_cache', ['region_code'], unique=False)
    op.create_index(op.f('ix_cards_cache_dat_hash'), 'cards_cache', ['dat_hash'], unique=False)
    op.create_index('ix_cards_cache_region_dat', 'cards_cache', ['region_code', 'dat_hash'], unique=False)

    # === clusters_cache ===
    op.create_table('clusters_cache',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column('region_code', sa.Integer(), nullable=False),
        sa.Column('settlement', sa.String(), nullable=False),
        sa.Column('year', sa.Integer(), nullable=False),
        sa.Column('method_version', sa.String(), nullable=False),
        sa.Column('payload', sa.JSONB(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_clusters_cache_region_code'), 'clusters_cache', ['region_code'], unique=False)
    op.create_index('ix_clusters_cache_region_settlement_year', 'clusters_cache', ['region_code', 'settlement', 'year'], unique=False)

    # === excel_cache ===
    op.create_table('excel_cache',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column('task_id', sa.String(), nullable=False),
        sa.Column('file1_bytes', sa.LargeBinary(), nullable=True),
        sa.Column('file2_bytes', sa.LargeBinary(), nullable=True),
        sa.Column('file1_path', sa.String(), nullable=True),
        sa.Column('file2_path', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_excel_cache_task_id'), 'excel_cache', ['task_id'], unique=False)

    # === osm_cache ===
    op.create_table('osm_cache',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column('query_hash', sa.String(), nullable=False),
        sa.Column('query_text', sa.Text(), nullable=False),
        sa.Column('response', sa.JSONB(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_osm_cache_query_hash'), 'osm_cache', ['query_hash'], unique=False)

    # === cameras_cache ===
    op.create_table('cameras_cache',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column('region_code', sa.Integer(), nullable=False),
        sa.Column('payload', sa.JSONB(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_cameras_cache_region_code'), 'cameras_cache', ['region_code'], unique=False)

    # === news_cache ===
    op.create_table('news_cache',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column('region_code', sa.Integer(), nullable=False),
        sa.Column('year', sa.Integer(), nullable=False),
        sa.Column('month', sa.Integer(), nullable=False),
        sa.Column('payload', sa.JSONB(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_news_cache_region_code'), 'news_cache', ['region_code'], unique=False)
    op.create_index('ix_news_cache_region_year_month', 'news_cache', ['region_code', 'year', 'month'], unique=False)


def downgrade() -> None:
    op.drop_table('news_cache')
    op.drop_table('cameras_cache')
    op.drop_table('osm_cache')
    op.drop_table('excel_cache')
    op.drop_table('clusters_cache')
    op.drop_table('cards_cache')
    op.drop_table('llm_messages')
    op.drop_table('llm_sessions')
    op.drop_table('access_log')
    op.drop_table('tasks')
