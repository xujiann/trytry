"""GET /metrics（Prometheus 文本导出）门控与格式。"""
from conftest import reset_database


def _client():
    from fastapi.testclient import TestClient

    from app.main import app

    reset_database()
    return TestClient(app)


def test_metrics_disabled_by_default_returns_404():
    from app.config import settings

    settings.metrics_export = False
    resp = _client().get("/metrics")
    assert resp.status_code == 404


def test_metrics_enabled_exposes_prometheus_text():
    from app.config import settings

    client = _client()
    settings.metrics_export = True
    try:
        client.get("/api/health")  # 产生一次计数
        resp = client.get("/metrics")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/plain")
        body = resp.text
        # 关键指标存在且为标准 exposition 结构（# TYPE + 样本行）
        assert "# TYPE medplat_requests_total counter" in body
        assert "medplat_requests_total{instance=" in body
        assert "medplat_up{instance=" in body
        # 无鉴权即可访问（Prometheus 抓取端不带 JWT），门控靠 config + 网络边界
        assert "medplat_uptime_seconds" in body
    finally:
        settings.metrics_export = False
