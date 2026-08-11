import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["MEDPLAT_DATABASE_URL"] = "sqlite:///./test_run.db"
# 附件测试落独立目录，避免污染开发环境 uploads/（.gitignore 均已排除）
os.environ["MEDPLAT_UPLOAD_DIR"] = "./test_uploads"


def reset_database():
    """各测试模块开始前重建库表，避免跨模块数据串扰。"""
    from app.database import Base, engine

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
