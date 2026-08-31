"""定时任务 `/api/jobs` 四个端点的**特征化网 + 响应契约**。

套路同 test_rules_contract.py / test_admin_mgmt_contract.py：先钉住**当前**响应的
完整 JSON（dict 相等）与键序 → 再加 `response_model` → 加完逐字节不变
（CLAUDE.md §11）。

本簇的建模判断：

- 任务清单行的 `last_run_at`/`next_run_at` 是 `isoformat()` **或空串**（从未跑过
  是 ""，不是 null）——两种取值本文件各钉一遍，声明 `str`。
- 触发回执与执行历史行是**两个形状**（6 键 vs 8 键，历史行多 trigger/created_at
  且键序不同）——两个模型，不许互相注入。
- `duration_ms`/`affected` 是 Integer 列 → int（`duration_ms` 值随执行耗时变，
  回绑后 type 钉死）。
- 清单不整表硬编码（任务注册表会随功能演进），钉：排序=按 name、键序、
  以及 access_log_archive 这一行的完整取值（它的调度参数是种子常量）。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app

JOB_ROW_KEYS = [
    "id", "name", "title", "interval_seconds", "enabled",
    "last_run_at", "next_run_at", "last_status", "implemented",
]
UPDATE_KEYS = ["name", "interval_seconds", "enabled"]
RUN_RECEIPT_KEYS = ["id", "job_name", "status", "message", "affected", "duration_ms"]
RUN_ROW_KEYS = ["id", "job_name", "trigger", "status", "message", "affected", "duration_ms", "created_at"]


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


def test_任务清单精确形状_未跑过是空串不是null(client, admin):
    rows = client.get("/api/jobs", headers=admin).json()
    assert rows, "启动种子应已登记任务"
    assert [list(r.keys()) for r in rows] == [JOB_ROW_KEYS] * len(rows)
    assert [r["name"] for r in rows] == sorted(r["name"] for r in rows)  # 按 name 排序
    target = next(r for r in rows if r["name"] == "access_log_archive")
    # 新库从未执行过：last_run_at/last_status 是空串（不是 null），next_run_at 已排期
    assert target == {
        "id": target["id"],
        "name": "access_log_archive",
        "title": "调阅留痕归档",
        "interval_seconds": 86400,
        "enabled": True,
        "last_run_at": "",
        "next_run_at": target["next_run_at"],
        "last_status": "",
        "implemented": True,
    }
    assert isinstance(target["next_run_at"], str) and "T" in target["next_run_at"]
    assert all(r["last_run_at"] == "" for r in rows)


def test_调参回执精确(client, admin):
    body = client.patch(
        "/api/jobs/access_log_archive", json={"interval_seconds": 43200}, headers=admin
    ).json()
    assert list(body.keys()) == UPDATE_KEYS
    assert body == {"name": "access_log_archive", "interval_seconds": 43200, "enabled": True}
    # 只给 enabled 的分支
    disabled = client.patch(
        "/api/jobs/access_log_archive", json={"enabled": False}, headers=admin
    ).json()
    assert disabled == {"name": "access_log_archive", "interval_seconds": 43200, "enabled": False}
    restored = client.patch(
        "/api/jobs/access_log_archive", json={"interval_seconds": 86400, "enabled": True},
        headers=admin,
    ).json()
    assert restored == {"name": "access_log_archive", "interval_seconds": 86400, "enabled": True}
    assert client.patch("/api/jobs/no_such_job", json={}, headers=admin).status_code == 404


def test_手动触发回执精确(client, admin):
    resp = client.post("/api/jobs/access_log_archive/run", headers=admin)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert list(body.keys()) == RUN_RECEIPT_KEYS
    # duration_ms 随执行耗时变（Integer 列，int），回绑后整 dict 相等
    assert type(body["duration_ms"]) is int and type(body["affected"]) is int
    assert body == {
        "id": body["id"],
        "job_name": "access_log_archive",
        "status": "succeeded",
        "message": "归档未启用（access_log_archive_days=0），跳过",
        "affected": 0,
        "duration_ms": body["duration_ms"],
    }
    assert client.post("/api/jobs/no_such_job/run", headers=admin).status_code == 404


def test_执行历史精确形状与键序(client, admin):
    resp = client.get("/api/jobs/runs", headers=admin)
    rows = resp.json()
    assert resp.headers["x-total-count"] == "1"
    assert [list(r.keys()) for r in rows] == [RUN_ROW_KEYS]
    row = rows[0]
    assert isinstance(row["created_at"], str) and "T" in row["created_at"]
    assert row == {
        "id": row["id"],
        "job_name": "access_log_archive",
        "trigger": "manual",
        "status": "succeeded",
        "message": "归档未启用（access_log_archive_days=0），跳过",
        "affected": 0,
        "duration_ms": row["duration_ms"],
        "created_at": row["created_at"],
    }
    assert client.get(
        "/api/jobs/runs?job_name=access_log_archive&status=succeeded", headers=admin
    ).json() == rows
    assert client.get("/api/jobs/runs?status=failed", headers=admin).json() == []
