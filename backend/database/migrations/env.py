import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------
# Ensure Alembic can find your app modules (database/, models/, etc.)
# ---------------------------------------------------------------------
sys.path.append(os.path.join(os.path.dirname(__file__), "..", ".."))

# Import your Base and engine
from database.session import Base, engine
import database.models  # noqa: F401  # ensure models are registered

# ---------------------------------------------------------------------
# Alembic Config setup
# ---------------------------------------------------------------------
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Provide metadata for 'autogenerate' support
target_metadata = Base.metadata


# ---------------------------------------------------------------------
# Helper to get the database URL dynamically
# ---------------------------------------------------------------------
def _get_url() -> str:
    """Return the database URL from environment or fallback to engine."""
    env_url = os.getenv("DATABASE_URL")
    if env_url:
        return env_url
    return str(engine.url)


# ---------------------------------------------------------------------
# Offline migrations (runs SQL scripts without connecting)
# ---------------------------------------------------------------------
def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = _get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


# ---------------------------------------------------------------------
# Online migrations (connects to the database)
# ---------------------------------------------------------------------
def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    config_section = config.get_section(config.config_ini_section, {})
    connectable = engine_from_config(
        config_section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        url=_get_url(),
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


# ---------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------
if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
