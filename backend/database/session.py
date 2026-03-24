import os
from contextlib import contextmanager
from typing import Generator, Optional

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import declarative_base, sessionmaker, Session

Base = declarative_base()


def _build_engine() -> Engine:
    raw_url = os.getenv("DATABASE_URL")
    if not raw_url or not raw_url.strip():
        raise RuntimeError(
            "DATABASE_URL must be set (e.g., postgresql+psycopg://user:pass@host:5432/testify or "
            "sqlite:///absolute/path/to/db)."
        )
    url = raw_url.strip()

    connect_args = {}
    if url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}

    engine = create_engine(
        url,
        echo=os.getenv("SQLALCHEMY_ECHO", "0") == "1",
        pool_pre_ping=True,
        connect_args=connect_args,
    )

    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, connection_record):  # type: ignore[unused-ignore]
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA foreign_keys=ON;")
            cursor.close()

    return engine


engine: Engine = _build_engine()
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a database session."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@contextmanager
def session_scope(expire_on_commit: Optional[bool] = None) -> Generator[Session, None, None]:
    """Context manager for scripts/CLI usage."""
    session_options = {}
    if expire_on_commit is not None:
        session_options["expire_on_commit"] = expire_on_commit
    session_factory = sessionmaker(bind=engine, autocommit=False, autoflush=False, **session_options)
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db_optional() -> Generator[Optional[Session], None, None]:
    """Yield a database session when the connection is available; otherwise, yield None."""
    try:
        for session in get_db():
            yield session
    except RuntimeError:
        yield None
