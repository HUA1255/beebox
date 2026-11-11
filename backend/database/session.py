from __future__ import annotations

import threading

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import config
from backend.database.migrations import run_migrations
from backend.database.models import Base

engine = None
SessionLocal = None
_current_db_path = None
# API request threads, the task worker and media helpers can all reach the
# first init_db() at once; two concurrent create_all() calls on a fresh SQLite
# file race on CREATE TABLE. Serialise the one-time setup.
_init_lock = threading.RLock()


def init_db() -> None:
    global engine, SessionLocal, _current_db_path
    with _init_lock:
        db_path = config.get_db_path()
        if engine is not None and _current_db_path == db_path:
            return
        if engine is not None:
            engine.dispose()
        engine = create_engine(
            f"sqlite:///{db_path}",
            # SQLite 默认 busy_timeout=0，API 线程与转写 worker 并发写时会直接
            # 报 database is locked；这里给足等待时间，让写者串行完成。
            connect_args={"check_same_thread": False, "timeout": 30},
        )
        _current_db_path = db_path
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        Base.metadata.create_all(bind=engine)
        run_migrations(engine, SessionLocal)


def get_db():
    init_db()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def seed_builtin_providers() -> None:
    """v1 has no external ASR provider surface; seeding is intentionally inert.

    Kept as a no-op so older callers/imports cannot resurrect provider rows.
    """
    return


def run_lightweight_migrations() -> None:
    run_migrations(engine, SessionLocal)
