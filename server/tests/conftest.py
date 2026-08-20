import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["MEDPLAT_DATABASE_URL"] = "sqlite:///./test_run.db"
# 附件测试落独立目录，避免污染开发环境 uploads/（.gitignore 均已排除）
os.environ["MEDPLAT_UPLOAD_DIR"] = "./test_uploads"
# 测试需要 console 通道回显 debug_code 拿验证码；生产默认关（sms_debug_echo=False）。
os.environ.setdefault("MEDPLAT_SMS_DEBUG_ECHO", "true")


def reset_database():
    """各测试模块开始前重建库表，避免跨模块数据串扰。"""
    from app.database import Base, engine

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


# ---------- 块5：E2E 开关（Playwright 浏览器全链路，默认跳过） ----------


def pytest_addoption(parser):
    parser.addoption(
        "--e2e",
        action="store_true",
        default=False,
        help="运行 Playwright 端到端测试（需已装 playwright 与 chromium 内核）",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "e2e: Playwright 浏览器端到端测试，默认跳过，需 --e2e 开启"
    )


def pytest_collection_modifyitems(config, items):
    """未传 --e2e 时，标记 e2e 的用例统一跳过（保持默认全量测试可离线快速跑完）。"""
    if config.getoption("--e2e"):
        return
    skip_e2e = pytest.mark.skip(reason="端到端测试默认跳过，使用 --e2e 开启")
    for item in items:
        if "e2e" in item.keywords:
            item.add_marker(skip_e2e)
