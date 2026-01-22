"""
app.api.db_debug
----------------
DB connectivity checks (MVP only).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_db

router = APIRouter()

@router.get("/db/ping")
def db_ping(db: Session = Depends(get_db)):
    row = db.execute(text("SELECT 1 AS ok, now() AS server_time")).mappings().one()
    return {"ok": row["ok"], "server_time": str(row["server_time"])}
