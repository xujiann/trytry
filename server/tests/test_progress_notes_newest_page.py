"""病程记录清单升序取前 100 条：一次住院记满 100 条之后，新写的病程在两端清单里都看不到（P2-362）。

`GET /api/inpatient/admissions/{id}/progress-notes` 按编号升序、默认 `limit=100`：截掉的恰好是最新的那一端。桌面端与
医生移动端都取第一页再倒过来显示「最新在前」——新写的病程提示「已记录」，清单里却一直没有。与体温单 P1-81 同一形状。
修法照 P1-81：从最近一条往前翻页，每页内仍按先后升序；不超过一页时逐字节不变。
"""
from app.database import SessionLocal


def _admission(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2362 病程医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2362 住院患者", "id_card": "330106197006062362"}).json()["id"]
    ward = client.post("/api/inpatient/wards", json={"name": "P2362 病区", "org_id": org}, headers=admin).json()
    bed = client.post("/api/inpatient/beds", json={"ward_id": ward["id"], "bed_no": "P2362-1"}, headers=admin).json()
    adm = client.post("/api/inpatient/admissions", headers=admin, json={
        "patient_id": patient, "org_id": org, "ward_id": ward["id"], "bed_id": bed["id"],
        "doctor_name": "P2362 医生", "diagnosis_name": "肺炎"})
    assert adm.status_code == 201, adm.text
    return adm.json()["id"]


def test_记满一页之后_第一页是最近的_页内照旧升序(client, admin):
    from app.models import ProgressNote, User

    adm = _admission(client, admin)
    with SessionLocal() as db:
        author = db.query(User).filter(User.username == "admin").one().id
        db.add_all([ProgressNote(admission_id=adm, note_type="daily", content=f"第{i}条", doctor_name="P2362 医生",
                                 created_by=author) for i in range(1, 102)])
        db.commit()
    got = client.get(f"/api/inpatient/admissions/{adm}/progress-notes", headers=admin)
    assert got.status_code == 200, got.text
    contents = [n["content"] for n in got.json()]
    assert len(contents) == 100 and got.headers["x-total-count"] == "101"
    assert contents[0] == "第2条" and contents[-1] == "第101条"   # 修前 第1条 … 第100条：最新那条看不到
    older = client.get(f"/api/inpatient/admissions/{adm}/progress-notes?offset=100", headers=admin).json()
    assert [n["content"] for n in older] == ["第1条"]
