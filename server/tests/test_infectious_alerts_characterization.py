"""特征化测试——保护 infectious 的 /alerts 与 /late-reports 从裸 dict 迁移到 response_model。

infectious 的 diseases/cases 端点已有契约（用 schemas），只剩这两个聚合端点是裸 dict。
迁移目标是加 response_model，响应字节必须不变（CLAUDE.md 第7条）。本测试钉住两端点
迁移前的精确键集合，迁移后仍须全绿。配方见 docs/接口标准与治理.md。
"""
from __future__ import annotations

import pytest

from conftest import login


ALERT_KEYS = {"disease_code", "disease_name", "case_count", "org_count", "window_days", "severity"}
LATE_KEYS = {
    "case_id", "org_id", "disease_code", "disease_name", "category",
    "report_hours", "onset_date", "reported_at", "days_late",
}


@pytest.fixture(scope="module")
def ctx(client):
    admin = login(client, "admin", "admin123")
    org = client.post(
        "/api/organizations",
        json={"name": "传染病特征化院", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()
    client.post(
        "/api/users",
        json={"username": "inf_ph", "password": "pass123456", "role": "public_health", "org_id": org["id"]},
        headers=admin,
    )
    ph = login(client, "inf_ph", "pass123456")
    # A15=肺结核（乙类 24h，启动种子）；发病日取较早，reported_at=now → 迟报 + 落入宽窗
    r = client.post(
        "/api/infectious/cases",
        json={"org_id": org["id"], "disease_code": "A15", "disease_name": "肺结核",
              "patient_name": "结核患者", "onset_date": "2026-07-01"},
        headers=ph,
    )
    assert r.status_code == 201, r.text
    return {"ph": ph, "org": org}


def test_alerts_键恰好(ctx, client):
    rows = client.get(
        "/api/infectious/alerts?window_days=60&threshold=1&today=2026-08-18", headers=ctx["ph"]
    ).json()
    assert rows, "宽窗低阈值下应有预警"
    for row in rows:
        assert set(row.keys()) == ALERT_KEYS, f"键漂移：{set(row.keys())}"
        assert row["severity"] in ("high", "medium")


def test_late_reports_键恰好(ctx, client):
    rows = client.get("/api/infectious/late-reports", headers=ctx["ph"]).json()
    assert rows, "旧发病日 + 当日上报应判迟报"
    for row in rows:
        assert set(row.keys()) == LATE_KEYS, f"键漂移：{set(row.keys())}"
