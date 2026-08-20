"""Alembic environment configuration for GIBDD Mini App migrations.

This module configures the Alembic migration environment:
- Loads DATABASE_URL from environment or .env file
- Configures target_metadata for autogenerate support (using SQLAlchemy Core)
- Runs migrations in transactional mode
"""

from logging.config import fileConfig
import os
import sys
from pathlib import Path

from sqlalchemy import engine_from_config, pool, MetaData
from alembic import context

# Add project root to path so we can import models if needed
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Define metadata for autogenerate support
# Since the project uses raw SQL schema (schema.sql), we create a MetaData
# object that will be populated by reflecting the database or by manual definitions
# For Phase 0, we'll use a simple approach - reflect from database or define manually
target_metadata = MetaData()

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def get_url():
    """Get database URL from environment or .env file."""
    # Try direct environment variable first (works in bothost and production)
    url = os.getenv("DATABASE_URL")
    
    if url:
        return url
    
    # Fallback: load from .env file (for local development)
    try:
        from dotenv import load_dotenv
        env_path = PROJECT_ROOT / ".env"
        if env_path.exists():
            load_dotenv(env_path)
            url = os.getenv("DATABASE_URL")
    except ImportError:
        pass
    
    if url:
        return url
    
    # Default for local development
    return "postgresql://postgres:postgres@localhost:5432/gibdd_db"


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.
    """
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.
    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        url=get_url(),
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            include_schemas=True,
            # For projects using raw SQL schema, we can still track changes
            # by comparing against reflected metadata
            render_as_batch=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
