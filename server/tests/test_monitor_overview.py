"""回归测试：/api/monitor/overview 的 recent_failures 只应含真正失败的任务。

修的 bug：读取端 `routers/monitor.py` 过滤 `status != "success"`，但写入端与模型默认
用的是 `"succeeded"`（`scheduler.py`、`models.py`）。因为库里没有一行是 `"success"`，
`!= "success"` 命中**每一行**，于是运维面板把每次成功执行都当成"最近失败"列出。

本测试播种一条成功 + 一条失败，断言 recent_failures **只含失败那条**。
改之前它应当变红（证明 bug 真实），改之后转绿（证明修好）。

端点只取**最近 5 条**失败。每条用例先清空 `job_runs` 再播种自己的数据：否则别的
用例（或调度线程真跑了任务）留下的失败记录会把 seeded 行挤出这个窗口，断言随执行
顺序时红时绿。清表也让"只返回最近 5 条"这条语义本身可被直接钉住。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app
from app.database import SessionLocal
from app.models import JobRun

#: 端点 recent_failures 的返回条数上限（与 routers/monitor.py 保持一致）
RECENT_FAILURES_LIMIT = 5


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


def _seed_runs(*runs: tuple[str, str]) -> None:
    """清空 job_runs 后按给定顺序播种 (job_name, status)，让窗口内容完全确定。"""
    with SessionLocal() as db:
        db.query(JobRun).delete()
        for name, status in runs:
            db.add(JobRun(job_name=name, trigger="manual", status=status, message=name))
        db.commit()


def _failures(client, admin) -> list[dict]:
    resp = client.get("/api/monitor/overview", headers=admin)
    assert resp.status_code == 200, resp.text
    return resp.json()["scheduler"]["recent_failures"]


def test_recent_failures_只含失败任务(client, admin):
    _seed_runs(("job_ok", "succeeded"), ("job_bad", "failed"))
    names = {r["name"] for r in _failures(client, admin)}
    assert names == {"job_bad"}, (
        "recent_failures 应恰好只含失败那条"
        "（bug：status 比对写成了 success 而非 succeeded，导致成功记录也被列出）"
    )


def test_recent_failures_每项状态都不是成功(client, admin):
    _seed_runs(("job_ok", "succeeded"), ("job_bad", "failed"))
    for row in _failures(client, admin):
        assert row["status"] != "succeeded", f"recent_failures 里混进了成功记录：{row}"


def test_recent_failures_只返回最近五条(client, admin):
    """窗口语义：失败多于 5 条时，只返回**最新**的 5 条（按 id 倒序）。

    这条同时守住上面两条用例的前提——它们靠清表保证 seeded 行落在窗口内，
    而窗口大小一旦改动，这里会先红。
    """
    total = RECENT_FAILURES_LIMIT + 2
    _seed_runs(*[(f"job_fail_{i}", "failed") for i in range(total)])
    rows = _failures(client, admin)
    assert len(rows) == RECENT_FAILURES_LIMIT
    newest = [f"job_fail_{i}" for i in range(total - 1, total - 1 - RECENT_FAILURES_LIMIT, -1)]
    assert [r["name"] for r in rows] == newest, "应按 id 倒序返回最新的失败记录"
