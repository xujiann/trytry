"""绩效整改任务一节：待办不再只看「最新一页」，退回理由与确认人显示出来（P2-462）。

① 清单只回最新 100 条，「确认关闭 / 退回」「登记进展 / 提交完成」只摆在这一页里：一条待确认的任务后面又下了
   100 条，它就被挤出窗口，页面上再没有一行能确认——与 P2-456 同一个毛病、同一个修法：没关闭的三种状态
   按状态单独取、排在最前、按 id 去重（core.js `actionableFirst`）。
② 退回之后接口里有 verify_comment / verified_by，页面一个都不显示；措施 / 结果一格一律 `completion_note || measures`，
   被驳回的那句「已整改完毕」还挂在整改中的任务上。现在按状态取：整改中的显示当前措施并带上退回人与理由，
   已关闭的带上确认人与意见。

operator 调得通「登记进展」、却进不了这一页（页面只开给管理层）属口径项，另行登记，不在本条。
"""
import os
import re

import pytest

from app.database import SessionLocal

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")


def _source() -> str:
    with open(os.path.join(STATIC, "pages-public.js"), encoding="utf-8") as fh:
        return fh.read()


def _function(name: str) -> str:
    source = _source()
    start = re.search(rf"^(?:async )?function {name}\(", source, re.M).start()
    end = re.compile(r"^}", re.M).search(source, start).end()
    return source[start:end]


def test_没关闭的三种状态单独取_排在最前():
    body = _function("drawImprovementTasks")
    assert ('["completed", "open", "in_progress"].map((st) => api(`/api/performance/improvements?status=${st}`))'
            in body)   # 修前只取最新一页
    assert "const tasks = actionableFirst(recent, ...open);" in body


def test_措施结果一格按状态取_带上退回人与理由():
    body = _function("drawImprovementTasks")
    assert "${improvementNote(t)}" in body
    assert "t.completion_note || t.measures) || " not in body.replace("esc(", "")   # 修前一律先取整改结果说明
    note = _function("improvementNote")
    assert "t.verify_comment" in note and "t.verified_by" in note
    assert "esc(t.verified_by)" in note and "esc(t.verify_comment)" in note
    # 整改中的只显示当前措施：被驳回的整改结果说明不再挂在上面
    assert "submitted ? (t.completion_note || t.measures) : t.measures" in note


def test_接口端_第101条之前待确认的那条_按状态取得到_退回理由与退回人都在出参里(client, admin):
    from sqlalchemy import insert

    from app.models import ImprovementTask

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2462 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    first = client.post("/api/performance/improvements", headers=admin, json={
        "org_id": org, "problem": "P2462 随访率偏低", "owner_name": "张三", "due_date": "2030-01-01"}).json()["id"]
    done = client.post(f"/api/performance/improvements/{first}/progress", headers=admin,
                       json={"complete": True, "completion_note": "已整改完毕"})
    assert done.status_code == 200 and done.json()["status"] == "completed", done.text
    with SessionLocal() as db:
        creator = db.get(ImprovementTask, first).created_by
        db.execute(insert(ImprovementTask), [{"org_id": org, "problem": f"P2462 其余 {i}", "owner_name": "李四",
                                              "due_date": "2030-01-01", "status": "verified", "created_by": creator}
                                             for i in range(100)])
        db.commit()
    recent = [t["id"] for t in client.get("/api/performance/improvements", headers=admin).json()]
    completed = client.get("/api/performance/improvements", headers=admin, params={"status": "completed"}).json()
    assert first not in recent   # 挤出了最新一页：修前页面上没有它的「确认关闭 / 退回」
    assert first in [t["id"] for t in completed]

    back = client.post(f"/api/performance/improvements/{first}/verify", headers=admin,
                       json={"approve": False, "comment": "佐证不足，请补充照片"})
    assert back.status_code == 200, back.text
    row = back.json()
    assert row["status"] == "in_progress"
    assert row["verify_comment"] == "佐证不足，请补充照片" and row["verified_by"]
    # 被驳回的整改结果说明还在库里：页面若仍先取它，整改中的任务上挂着的就是那句「已整改完毕」
    assert row["completion_note"] == "已整改完毕"


@pytest.mark.parametrize("status", ["completed", "open", "in_progress"])
def test_按状态取的三种都是接口认的状态(client, admin, status):
    resp = client.get("/api/performance/improvements", headers=admin, params={"status": status})
    assert resp.status_code == 200, resp.text
    assert all(t["status"] == status for t in resp.json())
