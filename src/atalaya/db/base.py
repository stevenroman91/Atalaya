"""Motor SQLAlchemy. SQLite por defecto, PostgreSQL vía DATABASE_URL (Railway)."""
from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        data_dir = Path(os.environ.get("ATALAYA_DATA_DIR", "data"))
        data_dir.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{data_dir / 'atalaya.db'}"
    # Railway expone postgres:// ; SQLAlchemy 2 + psycopg3 necesita postgresql+psycopg://
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


DATABASE_URL = _database_url()

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)

if DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_session():
    """Dependencia FastAPI / context manager de sesión."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
