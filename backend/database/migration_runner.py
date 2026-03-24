import os
import zlib
import logging

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine, Connection


_AUTO_MIGRATE_ENV = "AUTO_MIGRATE"
_LOCK_NAME = b"testify_alembic_migration_lock"


def auto_migrate_enabled() -> bool:
    """Return True when AUTO_MIGRATE explicitly enables migrations."""
    value = os.getenv(_AUTO_MIGRATE_ENV, "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _alembic_head_revision(config: Config) -> str:
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    if not heads:
        raise RuntimeError("Alembic head revision not found.")
    if len(heads) > 1:
        raise RuntimeError(f"Multiple Alembic heads detected: {heads}")
    return heads[0]


def _current_db_revision(connection: Connection) -> str | None:
    inspector = inspect(connection)
    if "alembic_version" not in set(inspector.get_table_names()):
        return None
    row = connection.execute(text("SELECT version_num FROM alembic_version")).first()
    return row[0] if row else None


def _advisory_lock_key() -> int:
    return int(zlib.crc32(_LOCK_NAME)) & 0x7FFFFFFF


def _acquire_advisory_lock(connection: Connection) -> int | None:
    if connection.dialect.name != "postgresql":
        return None
    lock_key = _advisory_lock_key()
    connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": lock_key})
    return lock_key


def _release_advisory_lock(connection: Connection, lock_key: int | None) -> None:
    if lock_key is None or connection.dialect.name != "postgresql":
        return
    connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key})


def run_migrations_if_needed(
    engine: Engine,
    config: Config,
    logger: logging.Logger | None = None,
) -> bool:
    """Run Alembic upgrade head once, guarded by a DB-level lock."""
    log = logger or logging.getLogger("alembic.migration")
    with engine.connect() as connection:
        lock_key = _acquire_advisory_lock(connection)
        try:
            current = _current_db_revision(connection)
            head = _alembic_head_revision(config)
            if current == head and current is not None:
                log.info("Alembic already at head %s; skipping migration.", head)
                return False

            log.warning("Running migrations (AUTO_MIGRATE enabled). Upgrading to %s.", head)
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            log.info("Alembic upgrade completed.")
            return True
        finally:
            _release_advisory_lock(connection, lock_key)
