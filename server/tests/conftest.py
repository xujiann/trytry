import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["MEDPLAT_DATABASE_URL"] = "sqlite:///./test_run.db"


def reset_database():
    """各测试模块开始前重建库表，避免跨模块数据串扰。"""
    from app.database import Base, engine

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
