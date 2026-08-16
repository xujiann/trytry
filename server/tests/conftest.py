import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
# 数据库隔离：
# - 外部已设 MEDPLAT_DATABASE_URL（如在 PG 上跑业务套）时尊重之，不覆盖；
# - 否则默认本地 SQLite，并按 pytest-xdist worker 分文件，避免并行 `-n auto` 时
#   多 worker 共用同一 test_run.db 互相踩（旧实现固定单文件，只能串行）。
_worker = os.environ.get("PYTEST_XDIST_WORKER", "")
_suffix = f"_{_worker}" if _worker else ""
if not os.environ.get("MEDPLAT_DATABASE_URL"):
    os.environ["MEDPLAT_DATABASE_URL"] = f"sqlite:///./test_run{_suffix}.db"
# 附件测试落独立目录（同样按 worker 隔离），避免污染开发环境 uploads/（.gitignore 均已排除）
os.environ.setdefault("MEDPLAT_UPLOAD_DIR", f"./test_uploads{_suffix}")


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
