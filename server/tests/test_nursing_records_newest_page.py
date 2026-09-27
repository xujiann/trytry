"""住院护理记录清单升序取前 100 条：一次住院记满 100 条之后，新写的护理记录在清单里看不到（P2-451）。

`GET /api/inpatient/admissions/{id}/nursing-records` 按编号升序、默认 `limit=100`：截掉的恰好是最新的那一端。住院文书页
「护理记录（N）」只取第一页——记满 100 条之后新记的提示成功，清单里却一直没有（`X-Total-Count` 是 101）。同一张页面上
的病程记录（P2-362）与体温单（P1-81）早已改成从最近一条往前翻，这一条漏了。修法同它们：从最近一条往前翻页，每页内
仍按先后升序；不超过一页时逐字节不变。
"""
from app.database import SessionLocal


def _admission(client, admin, n):
    org = client.post("/api/organizations", headers=admin, json={
        "name": f"P2451 护理医院{n}", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": f"P2451 住院患者{n}", "id_card": f"33010619700707245{n}"}).json()["id"]
    ward = client.post("/api/inpatient/wards", json={"name": f"P2451 病区{n}", "org_id": org}, headers=admin).json()
    bed = client.post("/api/inpatient/beds", json={"ward_id": ward["id"], "bed_no": f"P2451-{n}"}, headers=admin).json()
    adm = client.post("/api/inpatient/admissions", headers=admin, json={
        "patient_id": patient, "org_id": org, "ward_id": ward["id"], "bed_id": bed["id"],
        "doctor_name": "P2451 医生", "diagnosis_name": "肺炎"})
    assert adm.status_code == 201, adm.text
    return adm.json()["id"]


def test_记满一页之后_第一页是最近的_页内照旧升序(client, admin):
    from app.models import NursingRecord, User

    adm = _admission(client, admin, 1)
    with SessionLocal() as db:
        author = db.query(User).filter(User.username == "admin").one().id
        db.add_all([NursingRecord(admission_id=adm, nursing_level="level2", content=f"第{i}条", nurse_name="P2451 护士",
                                  created_by=author) for i in range(1, 102)])
        db.commit()
    got = client.get(f"/api/inpatient/admissions/{adm}/nursing-records", headers=admin)
    assert got.status_code == 200, got.text
    contents = [r["content"] for r in got.json()]
    assert len(contents) == 100 and got.headers["x-total-count"] == "101"
    assert contents[0] == "第2条" and contents[-1] == "第101条"   # 修前 第1条 … 第100条：最新那条看不到
    older = client.get(f"/api/inpatient/admissions/{adm}/nursing-records?offset=100", headers=admin).json()
    assert [r["content"] for r in older] == ["第1条"]


def test_不满一页_顺序照旧(client, admin):
    adm = _admission(client, admin, 2)
    for i in (1, 2, 3):
        created = client.post(f"/api/inpatient/admissions/{adm}/nursing-records", headers=admin,
                              json={"nursing_level": "level1", "content": f"护理{i}"})
        assert created.status_code == 201, created.text
    got = client.get(f"/api/inpatient/admissions/{adm}/nursing-records", headers=admin).json()
    assert [r["content"] for r in got] == ["护理1", "护理2", "护理3"]
