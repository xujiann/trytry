"""从复诊转出的待呼叫，复诊做完、被移除或随档案结束一并移除时撤出队列（P2-735，第十九批「逆操作是否撤净」扫描 K2-9）。

转呼叫接口写明随访 / 复诊 / 宣教 / 异常处置都能转（`ref_type` + `ref_id` 引用来源），可撤回帮手（P2-498）只撤
`ref_type=followup` 的：死亡收尾之后复诊已移除，外呼队列里仍挂着「王老汉 / 复诊 / 待呼叫」，坐席照单打给死者家属。
修法：撤回帮手按 (ref_type, ref_id) 泛化（`service.withdraw_calls`），档案结束一并移除复诊、手工移除 / 完成复诊时同样撤回。
"""
import pytest

from app.database import SessionLocal
from app.spd.models import SpdCallTask

B = "/api/spd"


def _login(client, username):
    token = client.post("/api/auth/login", json={"username": username, "password": "passw0rd1"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2735 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    created = client.post("/api/users", headers=admin, json={
        "username": "p2735_doc", "password": "passw0rd1", "full_name": "P2735 张医生", "role": "doctor", "org_id": org})
    assert created.status_code in (200, 201), created.text
    return {"org": org, "doctor": _login(client, "p2735_doc"), "n": 0}


def _patient_with_revisit_call(client, admin, world):
    world["n"] += 1
    patient = client.post("/api/patients", headers=admin, json={
        "name": f"P2735 患者{world['n']}", "id_card": f"33010219500101{2735 + world['n']:04d}"}).json()["id"]
    doctor = world["doctor"]
    client.post("/api/encounters", headers=doctor, json={"patient_id": patient, "org_id": world["org"],
                                                         "diagnosis_name": "高血压"})
    enrollment = client.post(f"{B}/enrollments", headers=doctor, json={
        "patient_id": patient, "program_code": "hypertension", "org_id": world["org"]})
    assert enrollment.status_code == 201, enrollment.text
    revisit = client.post(f"{B}/revisits", headers=doctor, json={
        "patient_id": patient, "program_code": "hypertension", "plan_date": "2026-12-01"})
    assert revisit.status_code == 201, revisit.text
    call = client.post(f"{B}/call-tasks", headers=doctor, json={
        "patient_id": patient, "ref_type": "revisit", "ref_id": revisit.json()["id"]})
    assert call.status_code == 201, call.text
    return enrollment.json()["id"], revisit.json()["id"], call.json()["id"]


def _call_status(call_id):
    with SessionLocal() as db:
        return db.get(SpdCallTask, call_id).status


def test_死亡收尾一并移除复诊_复诊转出的待呼叫撤回(client, admin, world):
    enrollment, _revisit, call = _patient_with_revisit_call(client, admin, world)
    closed = client.post(f"{B}/enrollments/{enrollment}/lifecycle", headers=world["doctor"],
                         json={"event": "death", "reason": "病故"})
    assert closed.status_code == 200, closed.text
    assert _call_status(call) == "withdrawn"   # 修前 pending：坐席照单打给死者家属


@pytest.mark.parametrize("status", ["removed", "done"], ids=["手工移除", "复诊完成"])
def test_手工移除或完成复诊_待呼叫撤回(client, admin, world, status):
    _enrollment, revisit, call = _patient_with_revisit_call(client, admin, world)
    body = {"status": status, **({"actual_date": "2026-09-29"} if status == "done" else {})}
    resp = client.patch(f"{B}/revisits/{revisit}", headers=world["doctor"], json=body)
    assert resp.status_code == 200, resp.text
    assert _call_status(call) == "withdrawn"   # 修前 pending


def test_改期不动待呼叫(client, admin, world):
    _enrollment, revisit, call = _patient_with_revisit_call(client, admin, world)
    resp = client.patch(f"{B}/revisits/{revisit}", headers=world["doctor"], json={"plan_date": "2026-12-15"})
    assert resp.status_code == 200, resp.text
    assert _call_status(call) == "pending"   # 改期后怎么处置呼叫与随访一侧同属 P2-672，不在这里定
