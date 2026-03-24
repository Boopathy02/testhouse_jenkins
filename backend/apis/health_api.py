from fastapi import APIRouter
from sqlalchemy import text

from database.session import engine


router = APIRouter()


@router.get("/health")
def health_check():
    status = "ok"
    db_status = "ok"
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        status = "degraded"
        db_status = "error"
    return {"status": status, "db": db_status}
