"""
Database engine and session helpers.

The worker can run without a database in file mode. Database mode is enabled
when DATABASE_URL is configured, for example:

    mysql+pymysql://adblock:adblock_pass@db:3306/adblock
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

try:
    from dotenv import load_dotenv

    backend_root = Path(__file__).resolve().parents[1]
    project_root = Path(__file__).resolve().parents[2]

    load_dotenv(backend_root / ".env.local")
    load_dotenv(backend_root / ".env")
    load_dotenv(project_root / ".env.local")
    load_dotenv(project_root / ".env")
except ImportError:
    pass

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker


DATABASE_URL = os.getenv("DATABASE_URL", "").strip()


class Base(DeclarativeBase):
    pass


if DATABASE_URL:
    engine = create_engine(
        DATABASE_URL,
        pool_pre_ping=True,
        pool_recycle=3600,
        future=True,
    )
else:
    engine = None


SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    future=True,
) if engine is not None else None


@contextmanager
def get_db() -> Generator:
    """
    Yield a SQLAlchemy session and close it afterwards.
    """
    if SessionLocal is None:
        raise RuntimeError("DATABASE_URL is not configured.")

    session = SessionLocal()

    try:
        yield session
    finally:
        session.close()


def init_db() -> None:
    """
    Create all known tables if they do not exist.
    """
    if engine is None:
        raise RuntimeError("DATABASE_URL is not configured.")

    from app import models  # noqa: F401  Ensure model metadata is registered.

    Base.metadata.create_all(bind=engine)
