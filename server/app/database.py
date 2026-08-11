from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings

DATABASE_URL = settings.database_url


def engine_kwargs(url: str) -> dict:
    """按连接串给出 create_engine 参数（可单测，不实连）。

    - SQLite：关闭同线程校验（TestClient/多线程），仅开发/单实例
    - PostgreSQL（postgresql:// / postgresql+psycopg2://）：无 SQLite 专属参数，
      连接池走 SQLAlchemy QueuePool 默认值，生产可按需扩展 pool_size 等
    """
    return {"connect_args": {"check_same_thread": False} if url.startswith("sqlite") else {}}


def build_engine(url: str):
    """构建数据库引擎：create_engine 惰性连接，首次执行 SQL 才真正建连。"""
    return create_engine(url, **engine_kwargs(url))


engine = build_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
