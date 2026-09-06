"""ADR-0019 续篇的回归：批量处理任务必须校验机构归属。

**又是"单条版对、批量版漏"**——这已经是第二次了：

| 单条（有守卫） | 批量（漏了） |
|---|---|
| `claim_candidate` → `assert_org_writable` | `distribute_candidates`（P1-42，已修） |
| `assign_task` → `assert_org_writable` | `batch_tasks`（本文件，P1-47） |

**实测出来的洞**（修之前）：建甲、乙两院与一个挂甲院的 `doctor`，往乙院落一条待办任务，
甲院 doctor 调 `POST /api/spd/tasks/batch` 取消它——

    乙院任务 id = 1 org_id = 2
    甲院 doctor 批量取消乙院任务 -> 200 {"processed":1,"skipped":[]}
       改后 status=cancelled review_note=甲院取消的

`action` 还支持 claim / urge / escalate / assign，也就是别家的待办可以被接走、催办、
升级或改派给任意人。

**越权不进 `skipped`，整批 403。** 这个接口的 docstring 明说"整批要么全成要么全败是错的口径"
——那说的是**业务**条件（任务已结束、已被他人接收），调用方看得到、也确实该继续办其余的。
机构归属不是业务条件：静默跳过会让调用方拿着 200 以为整批都办了，
而实际上他本来就不该碰那几条。
"""
from app.database import SessionLocal
from app.models import SpdTask
from conftest import login

B = "/api/spd"


def _org(client, admin, name, level="township"):
    return client.post("/api/organizations", headers=admin,
                       json={"name": name,
                             "org_type": "lead_hospital" if level == "county" else "township",
                             "level": level}).json()


def _task(patient_id: int, org_id: int, title: str) -> int:
    with SessionLocal() as db:
        t = SpdTask(patient_id=patient_id, org_id=org_id, task_type="followup",
                    title=title, status="pending", priority=1,
                    program_code="bt_hyp", source="manual")
        db.add(t)
        db.commit()
        return t.id


def _status(task_id: int) -> str:
    with SessionLocal() as db:
        return db.get(SpdTask, task_id).status


def test_批量取消别家的任务必须403且状态不变(client, admin):
    a = _org(client, admin, "批任甲院", "county")
    b = _org(client, admin, "批任乙院")
    client.post("/api/users", headers=admin,
                json={"username": "bt_doc_a", "password": "pass123456",
                      "role": "doctor", "org_id": a["id"]})
    doc_a = login(client, "bt_doc_a", "pass123456")
    p = client.post("/api/patients", headers=admin,
                    json={"name": "批任患者", "id_card": "330424199202021234",
                          "phone": "13700110061"}).json()
    tid = _task(p["id"], b["id"], "乙院的随访任务")

    resp = client.post(f"{B}/tasks/batch", headers=doc_a,
                       json={"task_ids": [tid], "action": "cancel", "note": "甲院取消的"})
    assert resp.status_code == 403, resp.text
    assert _status(tid) == "pending", "403 之后别家的任务状态不许变"


def test_一批里混进一条别家的任务就整批拒绝(client, admin):
    """越权不进 skipped：静默跳过会让调用方拿着 200 以为整批都办了。"""
    a = _org(client, admin, "批混甲院", "county")
    b = _org(client, admin, "批混乙院")
    client.post("/api/users", headers=admin,
                json={"username": "bt_doc_mix", "password": "pass123456",
                      "role": "doctor", "org_id": a["id"]})
    doc_a = login(client, "bt_doc_mix", "pass123456")
    p = client.post("/api/patients", headers=admin,
                    json={"name": "批混患者", "id_card": "330424199203031234",
                          "phone": "13700110062"}).json()
    mine = _task(p["id"], a["id"], "本院任务")
    theirs = _task(p["id"], b["id"], "别家任务")

    resp = client.post(f"{B}/tasks/batch", headers=doc_a,
                       json={"task_ids": [mine, theirs], "action": "cancel"})
    assert resp.status_code == 403, resp.text
    assert _status(mine) == "pending", "整批拒绝时本院那条也不许被改"


def test_批量处理本院任务照常放行(client, admin):
    a = _org(client, admin, "批本甲院", "county")
    client.post("/api/users", headers=admin,
                json={"username": "bt_doc_own", "password": "pass123456",
                      "role": "doctor", "org_id": a["id"]})
    doc_a = login(client, "bt_doc_own", "pass123456")
    p = client.post("/api/patients", headers=admin,
                    json={"name": "批本患者", "id_card": "330424199204041234",
                          "phone": "13700110063"}).json()
    tid = _task(p["id"], a["id"], "本院待办")

    resp = client.post(f"{B}/tasks/batch", headers=doc_a,
                       json={"task_ids": [tid], "action": "cancel", "note": "本院取消"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["processed"] == 1
    assert _status(tid) == "cancelled"


def test_全域角色跨机构批量处理仍然放行(client, admin):
    """admin/director 在 GLOBAL_ROLES 里，中心端批量调度是设计内的。"""
    b = _org(client, admin, "批全域乙院")
    p = client.post("/api/patients", headers=admin,
                    json={"name": "批全域患者", "id_card": "330424199205051234",
                          "phone": "13700110064"}).json()
    tid = _task(p["id"], b["id"], "乙院待办")

    resp = client.post(f"{B}/tasks/batch", headers=admin,
                       json={"task_ids": [tid], "action": "cancel", "note": "中心取消"})
    assert resp.status_code == 200, resp.text
    assert _status(tid) == "cancelled"
