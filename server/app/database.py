from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings

DATABASE_URL = settings.database_url


def engine_kwargs(url: str) -> dict:
    """按连接串给出 create_engine 参数（可单测，不实连）。

    - SQLite：关闭同线程校验（TestClient/多线程），仅开发/单实例；
      连接池参数不适用（NullPool/单文件），一律忽略。
    - PostgreSQL（postgresql:// / postgresql+psycopg2://）：连接池按
      `MEDPLAT_DB_POOL_*` 配置传入，0=沿用 SQLAlchemy QueuePool 默认
      （pool_size=5 / max_overflow=10 / 无超时 / 不回收）。取值建议（A11）：
      * pool_size = worker 数 × 每 worker 并发 ÷ 实例数，且全部实例合计
        不得超过 PG 的 max_connections 减去运维预留；
      * max_overflow 给突发余量（如 pool_size 的 1~2 倍）；
      * pool_timeout 让拿不到连接的请求**明确失败**而不是无限排队
        （否则过载时请求全部堆在池外，表现为整站假死）；
      * pool_recycle 设为略小于链路上最短的空闲断连时间（PG 侧
        idle_session_timeout、云负载均衡常见 300s），避免拿到已被
        对端关掉的死连接。
    """
    if url.startswith("sqlite"):
        return {"connect_args": {"check_same_thread": False}}
    kwargs: dict = {"connect_args": {}}
    if settings.db_pool_size > 0:
        kwargs["pool_size"] = settings.db_pool_size
    if settings.db_max_overflow > 0:
        kwargs["max_overflow"] = settings.db_max_overflow
    if settings.db_pool_timeout_seconds > 0:
        kwargs["pool_timeout"] = settings.db_pool_timeout_seconds
    if settings.db_pool_recycle_seconds > 0:
        kwargs["pool_recycle"] = settings.db_pool_recycle_seconds
    return kwargs


def build_engine(url: str):
    """构建数据库引擎：create_engine 惰性连接，首次执行 SQL 才真正建连。

    SQLite 每条连接建好即开外键约束（P2-71）：SQLite 默认**不查**外键，开发库、测试库于是收得下
    任何悬空 id——夹具写个占位 `patient_id=1` 照样绿、接口把请求体里不存在的编号原样写库也照样 201，
    同一份代码到生产 PG 上才撞外键。开了之后两边一个口径。只挂在应用自己的引擎上：alembic 迁移走
    `env.py` 自建的引擎，SQLite 上的批量改表（复制重建表）照旧在不查外键的连接上跑。
    """
    engine = create_engine(url, **engine_kwargs(url))
    if url.startswith("sqlite"):
        event.listen(engine, "connect", _sqlite_foreign_keys_on)
    return engine


def _sqlite_foreign_keys_on(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


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
