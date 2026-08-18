"""回归测试：/api/monitor/overview 的 recent_failures 只应含真正失败的任务。

修的 bug：读取端 `routers/monitor.py` 过滤 `status != "success"`，但写入端与模型默认
用的是 `"succeeded"`（`scheduler.py:104`、`models.py`）。因为库里没有一行是 `"success"`，
`!= "success"` 命中**每一行**，于是运维面板把每次成功执行都当成"最近失败"列出。

本测试播种一条成功 + 一条失败，断言 recent_failures **只含失败那条**。
改之前它应当变红（证明 bug 真实），改之后转绿（证明修好）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app
from app.database import SessionLocal
from app.models import JobRun


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def seeded(client):
    db = SessionLocal()
    try:
        db.add(JobRun(job_name="job_ok", trigger="manual", status="succeeded", message="done"))
        db.add(JobRun(job_name="job_bad", trigger="manual", status="failed", message="boom"))
        db.commit()
    finally:
        db.close()
    return True


def test_recent_failures_只含失败任务(client, admin, seeded):
    resp = client.get("/api/monitor/overview", headers=admin)
    assert resp.status_code == 200, resp.text
    names = {r["name"] for r in resp.json()["scheduler"]["recent_failures"]}
    assert "job_bad" in names, "失败任务应出现在 recent_failures 里"
    assert "job_ok" not in names, "成功任务不该被当成失败列出（bug：status 比对写成了 success 而非 succeeded）"


def test_recent_failures_每项状态都不是成功(client, admin, seeded):
    resp = client.get("/api/monitor/overview", headers=admin)
    for row in resp.json()["scheduler"]["recent_failures"]:
        assert row["status"] != "succeeded", f"recent_failures 里混进了成功记录：{row}"
