"""质控未判定、目标池待分发、无人认领三个「空值状态」清单接口能取出来，判据与工作台计数同一句（P2-825，第二十二批
「页面查询参数 vs 后端」扫描 X4-2；P2-782 当时点名「清单接口没有对应的筛选，不在此列」的那两份，加上任务一份）。

工作台三格都按空值状态计数：未判定的样本 `result` 是空串，待分发的目标患者没有团队也没有责任人，无人认领的任务没有责任人。
可清单接口表达不出「为空」——`list_qc_samples` 写 `if result:`，空串等于不筛；`list_candidates` 的 team_id /
assigned_user_id 是整数参数，传空串 422；`list_tasks` 取不到责任人为空的任务。页面只拿最新一页，窗口外的永远办不了：
一个批次抽出 80 条样本，把可见的 50 条判完刷新看到的还是这 50 条。修后三份各加一个筛选（`result=pending`、
`unassigned=true`），判据与工作台同一句（`service.task_unclaimed` / `candidate_undistributed`），页面单独取一遍排最前。
"""
import itertools
from pathlib import Path

import pytest

from app.database import SessionLocal
from app.spd import models as M

B = "/api/spd"
PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-spd.js").read_text(encoding="utf-8")
_SEQ = itertools.count(1)


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2825 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    doctor = client.post("/api/users", headers=admin, json={
        "username": "p2825_doc", "password": "pass123456", "role": "doctor", "org_id": org}).json()["id"]
    patients = [client.post("/api/patients", headers=admin, json={
        "name": f"P2825 患者{n}", "id_card": f"33010619650101{2825 + n:04d}"}).json()["id"] for n in range(4)]
    return {"org": org, "doctor": doctor, "patients": patients}


def test_质控样本_result_pending_取未判定的(client, admin, world):
    with SessionLocal() as db:
        records = [M.SpdFollowupRecord(patient_id=world["patients"][0], org_id=world["org"]) for _ in range(4)]
        db.add_all(records)
        db.flush()
        batch = f"P2825QC{next(_SEQ)}"   # 同一批次同一条随访只抽一次（唯一键），四条样本挂四条随访
        samples = [M.SpdQcSample(record_id=rec.id, batch=batch, result=r)
                   for rec, r in zip(records, ("", "pass", "", "fail"))]
        db.add_all(samples)
        db.commit()
        pending = {s.id for s in samples if not s.result}
    got = client.get(f"{B}/qc-samples", headers=admin, params={"batch": batch, "result": "pending", "limit": 100})
    assert got.status_code == 200, got.text
    assert {r["id"] for r in got.json()} == pending   # 修前 0 条（pending 不是库里的值）
    everyone = client.get(f"{B}/qc-samples", headers=admin, params={"batch": batch, "result": "", "limit": 100}).json()
    assert len(everyone) == 4   # 空串照旧是不筛


def test_目标患者_unassigned取待分发的_与工作台同一句(client, admin, world):
    with SessionLocal() as db:
        team = M.SpdTeam(name=f"P2825 团队{next(_SEQ)}", org_id=world["org"])
        db.add(team)
        db.flush()
        rows = [M.SpdCandidate(patient_id=world["patients"][n], program_code="hypertension", org_id=world["org"],
                               status=status, team_id=team_id, assigned_user_id=user_id)
                for n, (status, team_id, user_id) in enumerate((
                    ("target", None, None), ("target", team.id, None), ("target", None, world["doctor"]),
                    ("suspect", None, None)))]
        db.add_all(rows)
        db.commit()
        undistributed = rows[0].id
    got = client.get(f"{B}/candidates", headers=admin, params={
        "org_id": world["org"], "status": "target", "unassigned": "true", "limit": 100})
    assert got.status_code == 200, got.text
    assert [r["id"] for r in got.json()] == [undistributed]   # 修前参数不认，三条目标患者全回来
    total = client.get(f"{B}/candidates", headers=admin, params={"status": "target", "unassigned": "true", "limit": 1})
    center = client.get(f"{B}/workbench/center", headers=admin).json()
    assert int(total.headers["X-Total-Count"]) == center["pool"]["unassigned"]   # 与工作台「待分发」同一句


def test_任务_unassigned取无人认领的_与工作台同一句(client, admin, world):
    with SessionLocal() as db:
        tasks = [M.SpdTask(patient_id=world["patients"][1], org_id=world["org"], program_code="hypertension",
                           task_type="followup", title=f"P2825 {status}-{assignee}", status=status,
                           assignee_id=assignee, due_date="2099-12-01")
                 for status, assignee in (("pending", None), ("overdue", None), ("pending", world["doctor"]),
                                          ("claimed", world["doctor"]), ("done", None))]
        db.add_all(tasks)
        db.commit()
        unclaimed = {t.id for t in tasks[:2]}
    got = client.get(f"{B}/tasks", headers=admin, params={
        "patient_id": world["patients"][1], "unassigned": "true", "limit": 100})
    assert got.status_code == 200, got.text
    assert {r["id"] for r in got.json()} == unclaimed   # 修前参数不认，五条全回来
    total = client.get(f"{B}/tasks", headers=admin, params={"unassigned": "true", "limit": 1})
    center = client.get(f"{B}/workbench/center", headers=admin).json()
    assert int(total.headers["X-Total-Count"]) == center["todo"]["unassigned"]   # 与工作台「无人认领」同一句


def test_导出与清单同一个判据(client, admin, world):
    rows = client.get(f"{B}/tasks-export", headers=admin, params={"unassigned": "true", "limit": 5000}).json()
    total = client.get(f"{B}/tasks", headers=admin, params={"unassigned": "true", "limit": 1})
    assert rows["matched"] == int(total.headers["X-Total-Count"])   # 修前导出不认这个参数，勾着也导出全部
    assert all(r[8] in ("", None) for r in rows["rows"])   # 责任人ID 列全空


def test_页面单独取三份待办_任务表有责任人列():
    assert 'api("/api/spd/candidates?status=target&unassigned=true&limit=200")' in PAGE
    assert "const candidates = actionableFirst(recentCandidates, undistributed);" in PAGE
    assert 'api("/api/spd/qc-samples?result=pending&limit=200")' in PAGE
    assert "const qcSamples = actionableFirst(recentQc, pendingQc);" in PAGE
    assert '<input type="checkbox" name="unassigned" value="true"> 只看无人认领' in PAGE
    start = PAGE.index("const drawTasks = async (query) => {")
    draw = PAGE[start:PAGE.index("\n  };\n", start)]
    assert '"状态", "责任人", "优先级"' in draw and '<span class="tag orange">无人认领</span>' in draw
