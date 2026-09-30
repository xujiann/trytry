"""手工补建随访的「来源号」不核对、派生去重不看患者：另一位患者的出院随访被顶掉（P2-1018，第二十九批「字段之间的约束」扫描 E3-9）。

`followups.create_followup` 的来源号（多态：慢病档案 / 住院记录 / 手术申请 / 孕产妇档案）是谁的、是什么都不查；出院派生
`create_task` 按「类别 + 来源号 + 待随访」去重、不看患者。实测给甲补建出院随访、来源号写成乙这次住院号 201；乙出院 200，
乙名下 0 条出院随访，甲名下却多一条。页面补建不送来源号（缺省 0），对接方或直接调接口够得着。

修法：补建时来源号不为 0 就核对它是这位患者的这类单据，否则 422；派生去重键带上患者。
"""
import pytest

from app.database import SessionLocal
from app.models import FollowupTask
from app.routers.followups import create_task


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21018 县医院", "org_type": "lead_hospital", "level": "county"}).json()
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org["id"], "name": "P21018 病区"}).json()
    people, adms = [], []
    for i, card in enumerate(("330106196001011018", "330106196502021013")):
        patient = client.post("/api/patients", headers=admin, json={
            "name": f"P21018 患者{i}", "id_card": card, "gender": "男", "birth_date": "1960-01-01"}).json()
        bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward["id"], "bed_no": f"P21018-{i}"}).json()
        adm = client.post("/api/inpatient/admissions", headers=admin, json={
            "patient_id": patient["id"], "ward_id": ward["id"], "bed_id": bed["id"], "diagnosis_name": "肺炎"}).json()
        people.append(patient)
        adms.append(adm)
    return {"org": org, "people": people, "adms": adms}


def test_补建随访来源号不是这位患者的_422(client, admin, world):
    jia, yi = world["people"]
    got = client.post("/api/followups", headers=admin, json={
        "patient_id": jia["id"], "org_id": world["org"]["id"], "category": "discharge",
        "source_id": world["adms"][1]["id"], "due_date": "2026-10-10"})
    assert got.status_code == 422, got.text   # 修前 201，挂着乙这次住院号
    assert "不是这位患者的住院记录" in got.json()["detail"]
    missing = client.post("/api/followups", headers=admin, json={
        "patient_id": jia["id"], "org_id": world["org"]["id"], "category": "surgery",
        "source_id": 999999, "due_date": "2026-10-10"})
    assert missing.status_code == 422, missing.text


def test_补建随访来源号是自己的或不填_照收(client, admin, world):
    jia = world["people"][0]
    for source_id in (world["adms"][0]["id"], 0):
        got = client.post("/api/followups", headers=admin, json={
            "patient_id": jia["id"], "org_id": world["org"]["id"], "category": "discharge",
            "source_id": source_id, "due_date": "2026-10-10"})
        assert got.status_code == 201, got.text


def test_派生去重看患者_别人名下同来源号的待随访不顶掉这位的(client, world):
    jia, yi = world["people"]
    yi_adm = world["adms"][1]["id"]
    with SessionLocal() as db:
        # 存量：修之前补建进去的、来源号填成乙这次住院的一条甲的待随访
        db.add(FollowupTask(patient_id=jia["id"], org_id=world["org"]["id"], category="discharge",
                            source_id=yi_adm, title="出院随访", due_date="2026-10-10"))
        db.commit()
        task = create_task(db, patient_id=yi["id"], org_id=world["org"]["id"], category="discharge",
                           source_id=yi_adm, title="出院随访：肺炎", due_days=7)
        db.commit()
        assert task.patient_id == yi["id"]   # 修前取回的是甲那条
        mine = db.query(FollowupTask).filter(FollowupTask.patient_id == yi["id"], FollowupTask.category == "discharge").count()
        assert mine == 1
        again = create_task(db, patient_id=yi["id"], org_id=world["org"]["id"], category="discharge",
                            source_id=yi_adm, title="出院随访：肺炎", due_days=7)
        assert again.id == task.id   # 同一位患者、同一来源重跑照旧不重复派生
