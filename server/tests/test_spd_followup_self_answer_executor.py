"""居民自助作答的随访，执行人仍记在预先指派的医护名下（P2-936，第二十六批「同一业务动作的多个入口」扫描 H4-3）。

医护执行（`execute_followup`）把执行人写成实际执行的人；居民自助作答（`self_answer_followup`）只把渠道改成 self、
不动执行人——生成计划时指派的医护照旧挂着：人员工作量（`followup-stats` 的 `by_executor`）里算他完成一条，按执行人
筛的完成率把这条算成他的，质控抽查也把这条自助作答当成他的随访抽走。

修法：自助作答办结时执行人置空（居民自己答的，没有医护执行人），与执行路径「记实际执行的人」同一口径。
"""
import pytest

from app import clock
from app.database import SessionLocal
from app.models import User
from app.spd.models import SpdFollowupRecord
from conftest import login

B = "/api/spd"
PROGRAM = "p2936_htn"
PHONE = "13900029360"
ID_CARD = "330106196801010039"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2936 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    for name in ("nurse", "doctor"):
        made = client.post("/api/users", headers=admin, json={
            "username": f"p2936_{name}", "password": "passw0rd1", "full_name": f"P2936 {name}", "role": "doctor",
            "org_id": org})
        assert made.status_code in (200, 201), made.text
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2936 居民", "id_card": ID_CARD, "gender": "男", "birth_date": "1968-01-01", "phone": PHONE})
    assert patient.status_code in (200, 201), patient.text
    assert client.post(f"{B}/programs", headers=admin, json={
        "code": PROGRAM, "name": "P2936 高血压", "category": "chronic"}).status_code == 201
    code = client.post("/api/portal/auth/sms/code", json={"phone": PHONE}).json()["debug_code"]
    token = client.post("/api/portal/auth/sms/login", json={"phone": PHONE, "code": code}).json()["access_token"]
    with SessionLocal() as db:
        nurse = db.query(User.id).filter(User.username == "p2936_nurse").scalar()
        doctor = db.query(User.id).filter(User.username == "p2936_doctor").scalar()
        records = []
        for _ in range(2):   # 两条都指派给护士甲
            record = SpdFollowupRecord(patient_id=patient.json()["id"], program_code=PROGRAM, org_id=org,
                                       planned_at=clock.today().isoformat(), status="planned", executor_id=nurse)
            db.add(record)
            db.flush()
            records.append(record.id)
        db.commit()
    return {"org": org, "nurse": nurse, "doctor": doctor, "records": records,
            "ph": {"Authorization": f"Bearer {token}"}, "dh": login(client, "p2936_doctor", "passw0rd1")}


def test_自助作答_执行人置空_医护执行_记实际执行人(client, admin, world):
    first, second = world["records"]
    answered = client.post(f"/api/portal/spd/followups/{first}/self-answer", headers=world["ph"],
                           json={"answers": {"note": "一切正常"}})
    assert answered.status_code == 200, answered.text
    executed = client.post(f"{B}/followup-records/{second}/execute", headers=world["dh"],
                           json={"answers": {"note": "电话随访"}, "channel": "phone"})
    assert executed.status_code == 200, executed.text
    with SessionLocal() as db:
        one, two = db.get(SpdFollowupRecord, first), db.get(SpdFollowupRecord, second)
        assert (one.status, one.channel, one.executor_id) == ("done", "self", None)   # 修前仍是护士甲
        assert (two.status, two.executor_id) == ("done", world["doctor"])


def test_人员工作量与按执行人筛的完成率不算护士甲(client, admin, world):
    stats = client.get(f"{B}/followup-stats", headers=admin).json()
    workload = {row["executor_id"]: row["done"] for row in stats["by_executor"]}
    assert world["nurse"] not in workload   # 修前护士甲完成 1 条
    assert workload.get(world["doctor"]) == 1
    mine = client.get(f"{B}/followup-stats", headers=admin, params={"executor_id": world["nurse"]}).json()
    assert (mine["total"], mine["done"]) == (0, 0)   # 修前 1 / 1，完成率 100%
