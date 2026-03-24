"""Inspect or overwrite the alembic_version row using the project's SQLAlchemy engine.

Usage:
  # show current alembic_version
  python scripts/fix_alembic_version.py

  # set alembic_version to a specific revision (destructive) after confirmation
  python scripts/fix_alembic_version.py --set 20251219_0011 --yes

Always backup your database before changing migration metadata.
"""
from __future__ import annotations

import argparse
import sys
from sqlalchemy import text
from pathlib import Path
import sys
from dotenv import load_dotenv

# Ensure project's path imports work (add backend/ to sys.path)
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
# load env from backend/.env if present
load_dotenv(str(HERE.parent / ".env"))

from database.session import engine


def get_current_revision(conn) -> str | None:
    try:
        result = conn.execute(text("SELECT version_num FROM alembic_version"))
    except Exception:
        return None
    row = result.first()
    return row[0] if row else None


def set_revision(conn, rev: str) -> None:
    # If table exists with a row, update it; otherwise insert.
    try:
        existing = conn.execute(text("SELECT count(*) FROM alembic_version")).scalar()
    except Exception:
        # create table and insert
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        conn.execute(text("INSERT INTO alembic_version (version_num) VALUES (:rev)"), {"rev": rev})
        return

    if existing and existing > 0:
        conn.execute(text("UPDATE alembic_version SET version_num = :rev"), {"rev": rev})
    else:
        conn.execute(text("INSERT INTO alembic_version (version_num) VALUES (:rev)"), {"rev": rev})


def main(argv: list[str]):
    parser = argparse.ArgumentParser()
    parser.add_argument("--set", dest="set_rev", help="Revision to set alembic_version to")
    parser.add_argument("--yes", action="store_true", help="Confirm destructive action")
    args = parser.parse_args(argv)

    with engine.connect() as conn:
        current = get_current_revision(conn)
        print("Current alembic_version:", current)

        if args.set_rev:
            if not args.yes:
                print("Refusing to set revision without --yes. This is destructive.")
                sys.exit(2)
            print(f"Setting alembic_version to: {args.set_rev}")
            set_revision(conn, args.set_rev)
            print("Done.")


if __name__ == "__main__":
    main(sys.argv[1:])
