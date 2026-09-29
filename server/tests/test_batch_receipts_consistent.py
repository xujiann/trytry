"""批量回执对得上：不存在的编号列出来、重复的编号不算「不存在」、待审核的与单条同一句、同批重复的规则不悄悄覆盖（P2-733，
第十九批「批量 vs 单条」扫描 K1-8）。

- 慢专病批量处理：不存在的任务编号既不处理也不提，批量取消 [真, 99991, 99992] 回「处理 1、跳过 0」；批量接收别人提交在
  等审核的任务，跳过原因写「已被他人接收」，实际是在等审核（单条接收说「不处于可接收状态」）。
- 目标池分发：`not_found = 传入条数 − 查到条数`，同一条传 3 次回「分发 1、2 个编号不存在」，页面照印。
- 审方规则导入：同一药品编码两行（日剂量上限 10 与 1000），回「新建 1、更新 1」，现行规则变成 1000——文件里重复一行就
  把上限放宽 100 倍，回执看不出来。改为整批 422 点名（不替人挑哪一行作数）。
"""
import pytest

from app.database import SessionLocal
from app.spd.models import SpdCandidate, SpdTask

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2733 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    other = client.post("/api/users", headers=admin, json={
        "username": "p2733_other", "password": "passw0rd1", "full_name": "p2733_other", "role": "doctor",
        "org_id": org})
    assert other.status_code in (200, 201), other.text
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2733 患者", "id_card": "330102197001012733"}).json()["id"]
    return {"org": org, "other": other.json()["id"], "patient": patient}


def _task(client, admin, world):
    resp = client.post(f"{B}/tasks", headers=admin, json={
        "patient_id": world["patient"], "title": "P2733 随访", "org_id": world["org"]})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_批量处理_不存在的编号列进跳过(client, admin, world):
    real = _task(client, admin, world)
    resp = client.post(f"{B}/tasks/batch", headers=admin, json={"task_ids": [real, 99991, 99992], "action": "cancel"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"processed": 1, "skipped": [{"id": 99991, "reason": "任务不存在"},
                                                       {"id": 99992, "reason": "任务不存在"}]}   # 修前 skipped []


def test_批量接收_别人提交待审核的_与单条同一句(client, admin, world):
    task = _task(client, admin, world)
    with SessionLocal() as db:   # 别人已接收并提交、在等审核
        row = db.get(SpdTask, task)
        row.assignee_id, row.status = world["other"], "submitted"
        db.commit()
    resp = client.post(f"{B}/tasks/batch", headers=admin, json={"task_ids": [task], "action": "claim"})
    assert resp.json()["skipped"] == [{"id": task, "reason": "不处于可接收状态"}], resp.text   # 修前「已被他人接收」
    single = client.post(f"{B}/tasks/{task}/claim", headers=admin)
    assert (single.status_code, single.json()["detail"]) == (409, "该任务不处于可接收状态")


def test_目标池分发_同一编号传三次不算不存在(client, admin, world):
    with SessionLocal() as db:
        row = SpdCandidate(patient_id=world["patient"], program_code="hypertension", status="target",
                           org_id=world["org"], source="screening")
        db.add(row)
        db.commit()
        cid = row.id
    resp = client.post(f"{B}/candidates/distribute", headers=admin, json={"candidate_ids": [cid, cid, cid],
                                                                         "org_id": world["org"]})
    assert resp.json() == {"distributed": 1, "not_found": 0}, resp.text   # 修前 not_found 2


def test_审方规则导入_同批同一编码两行_整批422_一条不写(client, admin):
    payload = [{"drug_code": "P2733-D001", "max_daily_dose": 10}, {"drug_code": "P2733-D002", "max_daily_dose": 5},
               {"drug_code": "P2733-D001", "max_daily_dose": 1000}]
    resp = client.post("/api/prescriptions/rules/import", headers=admin, json=payload)
    assert resp.status_code == 422 and "P2733-D001" in resp.json()["detail"], resp.text   # 修前 200、上限变 1000
    codes = {r["drug_code"] for r in client.get("/api/prescriptions/rules", headers=admin).json()}
    assert not codes & {"P2733-D001", "P2733-D002"}
