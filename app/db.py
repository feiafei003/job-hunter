import logging
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine

from .config import get_settings

_settings = get_settings()
_engine = create_engine(
    f"sqlite:///{_settings.db_path}",
    connect_args={"check_same_thread": False},
)


def init_db() -> None:
    # 确保模型被导入并注册到元数据
    from . import models  # noqa: F401

    SQLModel.metadata.create_all(_engine)
    _run_migrations()


def _run_migrations() -> None:
    """轻量迁移：为旧库补齐新增列，避免删库丢数据。"""
    # (表名, 列名, 列定义)
    additions = [
        ("searchconfig", "date_range", "VARCHAR DEFAULT 'any'"),
        ("jobposting", "experience", "VARCHAR DEFAULT ''"),
        ("jobposting", "education", "VARCHAR DEFAULT ''"),
        ("jobposting", "tags", "VARCHAR DEFAULT ''"),
        ("analysis", "profile_hash", "VARCHAR DEFAULT ''"),
        ("analysis", "skills_to_learn", "VARCHAR DEFAULT ''"),
        ("analysis", "resume_tips", "VARCHAR DEFAULT ''"),
        ("subscription", "user_id", "INTEGER"),
        ("subscription", "max_jobs", "INTEGER DEFAULT 10"),
        ("subscription", "notify_empty", "BOOLEAN DEFAULT 0"),
        ("user", "resume_text", "VARCHAR DEFAULT ''"),
        ("user", "resume_filename", "VARCHAR DEFAULT ''"),
        ("user", "resume_updated_at", "DATETIME"),
    ]
    log = logging.getLogger("jobhunter.db")
    with _engine.begin() as conn:
        for table, column, ddl in additions:
            cols = {
                row[1]
                for row in conn.execute(text(f"PRAGMA table_info({table})"))
            }
            if column not in cols:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
                log.info("迁移：为 %s 增加列 %s", table, column)


def get_engine():
    return _engine


@contextmanager
def session_scope() -> Iterator[Session]:
    # expire_on_commit=False：提交后仍可在会话外读取已加载字段，
    # 避免 DetachedInstanceError（调度器与 API 序列化都依赖此行为）。
    session = Session(_engine, expire_on_commit=False)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
