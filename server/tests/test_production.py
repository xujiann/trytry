"""M1 生产化基础：统一配置、请求追踪、健康检查数据库探测。"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


def test_settings_reads_medplat_env():
    """pydantic-settings 兼容既有 MEDPLAT_* 环境变量。"""
    from app.config import settings

    assert settings.database_url == "sqlite:///./test_run.db"
    assert settings.admin_password  # 有默认值，环境可覆盖


def test_health_includes_database_check(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["database"] == "ok"


def test_request_id_generated_and_echoed(client):
    # 未传入时由服务端生成
    generated = client.get("/api/health").headers.get("x-request-id")
    assert generated

    # 传入 X-Request-ID 则原样透传，支持跨系统链路追踪
    echoed = client.get("/api/health", headers={"X-Request-ID": "trace-abc-123"})
    assert echoed.headers["x-request-id"] == "trace-abc-123"


def test_alembic_baseline_exists():
    """迁移体系就绪：存在初始基线迁移文件。"""
    from pathlib import Path

    versions = Path(__file__).resolve().parent.parent / "alembic" / "versions"
    assert versions.is_dir()
    assert any(p.suffix == ".py" for p in versions.iterdir())
