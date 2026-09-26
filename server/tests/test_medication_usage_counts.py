"""全县用药地图与用药画像把处方明细行当成「方」：同一味药换个写法拆成两行，同一张方里两行记成两张（P2-354）。

`usage_stats` 按（药品编码, 药名）分组、`count(明细行)`——药名是每行自由填的，同一个编码换个写法就拆成两行排名；
同一张处方里这味药有两行（审方后补开、分次用法）就记成两张方。页面标的是「方」，画像的「次」同样按行数。
修法：按编码归并，「方」数与画像的「次」都按处方数；药名取同编码里字典序最小的写法。
"""
from app.database import SessionLocal


def _approved(db, patient, org, user, lines):
    from app.models import Prescription, PrescriptionItem

    rx = Prescription(patient_id=patient, org_id=org, status="approved", created_by=user)
    db.add(rx)
    db.flush()
    for code, name in lines:
        db.add(PrescriptionItem(prescription_id=rx.id, drug_code=code, drug_name=name, daily_dose=1.0, days=30))


def test_同一味药归并_方数按处方数(client, admin):
    from app.models import User

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2354 用药卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2354 患者", "id_card": "330106197005052354"}).json()["id"]
    with SessionLocal() as db:
        user = db.query(User).filter(User.username == "admin").one().id
        _approved(db, patient, org, user, [("P2354MET", "二甲双胍"), ("P2354MET", "二甲双胍")])   # 一张方里两行
        _approved(db, patient, org, user, [("P2354MET", "二甲双胍片")])                         # 换个写法
        db.commit()
    rows = [r for r in client.get("/api/medication/usage-stats", headers=admin).json() if r["drug_code"] == "P2354MET"]
    assert rows == [{"drug_code": "P2354MET", "drug_name": "二甲双胍", "rx_count": 2, "patient_count": 1}]
    # 修前两行：(二甲双胍, 2 张方)、(二甲双胍片, 1 张方)
    profile = client.get(f"/api/medication/profile/{patient}", headers=admin).json()
    assert [(d["drug_code"], d["times"]) for d in profile["drugs"]] == [("P2354MET", 2)]   # 修前 3 次
