"""居民的专病服务申请还在「待受理」，医护直接签约建档后那条申请照挂「待受理」（P2-937，第二十六批「同一业务动作的
多个入口」扫描 H4-4）。

申请是给「未纳管居民」的（模型注释），在管病种的新申请 409（P2-559）；经「受理 → 建档」这条路，申请本身会被办结。
医护在门诊直接签约建档（`create_enrollment`）只把目标池改成已纳管、不动申请：居民端同时显示「已纳管」和「待受理」，
管理端工作台「待受理申请」建档前后都是 1；医生按清单驳回后，在管居民看到「已驳回」；点「受理」什么也不做。

修法：建档同一事务把这位居民这个病种的待受理申请办结为「已受理」，记处理人、时间与「已直接签约建档」。
别的病种、别的居民的申请不动。
"""
import pytest

from app.database import SessionLocal
from app.models import User
from app.spd.models import SpdServiceApply

B = "/api/spd"
PERSON = {"name": "P2937 居民", "id_card": "330106196606060057", "gender": "男", "birth_date": "1966-06-06",
          "phone": "13900029370"}
OTHER = {"name": "P2937 邻居", "id_card": "33010619670707006X", "gender": "女", "birth_date": "1967-07-07",
         "phone": "13900029371"}


def _resident(client, person):
    code = client.post("/api/portal/auth/sms/code", json={"phone": person["phone"]}).json()["debug_code"]
    resp = client.post("/api/portal/auth/sms/login", json={"phone": person["phone"], "code": code})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2937 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    ids, heads, applies = {}, {}, {}
    for key, person in (("me", PERSON), ("other", OTHER)):
        made = client.post("/api/patients", headers=admin, json=person)
        assert made.status_code in (200, 201), made.text
        ids[key] = made.json()["id"]
        heads[key] = _resident(client, person)
    for key, program in (("me", "diabetes"), ("me", "hypertension"), ("other", "diabetes")):
        applied = client.post("/api/portal/spd/service-applies", headers=heads[key], json={"program_code": program})
        assert applied.status_code == 201, applied.text
        applies[(key, program)] = applied.json()["id"]
    return {"org": org, "ids": ids, "heads": heads, "applies": applies}


def _apply(apply_id):
    with SessionLocal() as db:
        row = db.get(SpdServiceApply, apply_id)
        return row.status, row.handle_note, row.handled_by, row.handled_at


def test_直接签约建档_同病种的待受理申请办结为已受理(client, admin, world):
    pending_before = {a["id"] for a in client.get(f"{B}/service-applies", headers=admin).json()}
    assert world["applies"][("me", "diabetes")] in pending_before
    enrolled = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": world["ids"]["me"], "program_code": "diabetes", "org_id": world["org"]})
    assert enrolled.status_code == 201, enrolled.text
    status, note, handled_by, handled_at = _apply(world["applies"][("me", "diabetes")])
    with SessionLocal() as db:
        admin_id = db.query(User.id).filter(User.username == "admin").scalar()
    assert (status, note, handled_by) == ("accepted", "已直接签约建档", admin_id)   # 修前照挂 pending
    assert handled_at is not None
    pending_after = {a["id"] for a in client.get(f"{B}/service-applies", headers=admin).json()}
    assert world["applies"][("me", "diabetes")] not in pending_after   # 工作台「待受理」减一
    mine = client.get("/api/portal/spd/service-applies", headers=world["heads"]["me"]).json()
    assert {a["program_code"]: a["status"] for a in mine}["diabetes"] == "accepted"


def test_别的病种_别的居民的申请不动(client, world):
    assert _apply(world["applies"][("me", "hypertension")])[0] == "pending"
    assert _apply(world["applies"][("other", "diabetes")])[0] == "pending"
