"""
Database package exposing session helpers and models.

This module also registers backward-compatible aliases so legacy imports like
``import db`` or ``from db.session import get_db`` continue to work even though
all database-related files now live under ``backend/database``.
"""

import sys

from . import models as _models  # noqa: F401
from . import session as _session  # noqa: F401
from .session import get_db, SessionLocal, engine  # noqa: F401

# ---------------------------------------------------------------------------
# Backward-compatible module aliases (db -> database)
# ---------------------------------------------------------------------------
sys.modules.setdefault("db", sys.modules[__name__])
sys.modules["db.models"] = _models
sys.modules["db.session"] = _session
