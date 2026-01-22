"""
app.db
------
Database setup for C&W Board Evaluation (MVP).

- Reads DATABASE_URL from env
- Creates SQLAlchemy engine and session factory
- Exposes get_db() dependency for FastAPI
- Exposes init_db() to create tables (MVP only; later use Alembic migrations)
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
    """FastAPI dependency that yields a DB session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create tables (MVP convenience)."""
    from app.models import Base  # local import avoids circular imports

    Base.metadata.create_all(bind=engine)

def init_db_old() -> None:
    """Create tables (MVP convenience)."""
    #from app.db.models import Base  # ✅ correct path

    #Base.metadata.create_all(bind=engine)
