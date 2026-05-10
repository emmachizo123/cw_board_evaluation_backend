"""
app.db.session
--------------
SQLAlchemy session + engine setup (Postgres).

Exposes:
- engine
- SessionLocal
- get_db() dependency (FastAPI)
- init_db() — no-op; schema is applied with Alembic (`alembic upgrade head`)
"""

from __future__ import annotations

import os
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
if not DATABASE_URL:
    raise ValueError(
        "DATABASE_URL is required. Example: postgresql+psycopg://postgres:password@localhost:5432/cw_board_eval"
    )

engine = create_engine(DATABASE_URL, pool_pre_ping=True)

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a DB session and guarantees close()."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """
    Reserved hook (no-op). All DDL is managed by Alembic migrations.

    Before running the API locally or in CI:
      alembic upgrade head
    """
    return
