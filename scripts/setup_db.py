import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from alembic import command
from alembic.config import Config


def _sanitize_database_url(url: str | None) -> str:
    if not url:
        return "<not set>"
    try:
        from sqlalchemy.engine import make_url

        return make_url(url).render_as_string(hide_password=True)
    except Exception:
        return "<invalid>"


def _load_env(repo_root: Path) -> None:
    backend_env = repo_root / "backend" / ".env"
    root_env = repo_root / ".env"
    if backend_env.exists():
        load_dotenv(backend_env)
    if root_env.exists():
        load_dotenv(root_env)


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    _load_env(repo_root)

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL is not set. Configure it before running setup.", file=sys.stderr)
        return 1

    backend_root = repo_root / "backend"
    alembic_ini = backend_root / "database" / "alembic.ini"
    migrations_dir = backend_root / "database" / "migrations"

    if not alembic_ini.exists():
        print(f"Alembic config not found: {alembic_ini}", file=sys.stderr)
        return 1

    config = Config(str(alembic_ini))
    config.set_main_option("script_location", str(migrations_dir))
    config.set_main_option("sqlalchemy.url", db_url)

    print(f"Running Alembic migrations against {_sanitize_database_url(db_url)}")
    command.upgrade(config, "head")
    print("Database schema is up to date.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
