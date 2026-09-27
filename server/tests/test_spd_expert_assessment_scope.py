"""专家工作台的「评估人次 / 风险分布」与本页其余数字同一个范围、同一个病种（P2-552，第十批「同源数字承诺」扫描 X2-8）。

同一页的在管数、路径、转诊都按账号的机构范围与所选病种算，评估一格却是 `db.query(SpdAssessment).count()`——全县全病种。
`spd_assessments` 没有机构列，修后按范围内在管档案的患者归属，并按病种筛。
"""
import pytest


@pytest.fixture(scope="module")
def world(client, admin):
    from app.database import SessionLocal
    from app.spd.models import SpdAssessment, SpdScale

    orgs = {tag: client.post("/api/organizations", headers=admin, json={
        "name": f"P2552 {tag}院", "org_type": "township", "level": "township"}).json()["id"] for tag in ("甲", "乙")}
    patients = {}
    for n, tag in enumerate(("甲", "乙")):
        pid = client.post("/api/patients", headers=admin, json={
            "name": f"P2552 {tag}院患者", "id_card": f"33010619700101255{n}"}).json()["id"]
        resp = client.post("/api/spd/enrollments", headers=admin, json={
            "patient_id": pid, "program_code": "hypertension", "org_id": orgs[tag]})
        assert resp.status_code == 201, resp.text
        patients[tag] = pid
    with SessionLocal() as db:
        scale = db.query(SpdScale).filter(SpdScale.status == "published").order_by(SpdScale.id).first()
        for tag, risk in (("甲", "low"), ("乙", "high"), ("乙", "high")):
            db.add(SpdAssessment(patient_id=patients[tag], scale_id=scale.id, scale_code=scale.code,
                                 program_code="hypertension", risk_level=risk, score=1))
        db.commit()
    doctor = client.post("/api/users", headers=admin, json={
        "username": "p2552_doc", "password": "pass123456", "role": "doctor", "org_id": orgs["甲"]})
    assert doctor.status_code == 201, doctor.text
    token = client.post("/api/auth/login", json={"username": "p2552_doc", "password": "pass123456"}).json()
    return {"Authorization": f"Bearer {token['access_token']}"}


def test_评估人次按账号的机构范围(client, world):
    resp = client.get("/api/spd/workbench/expert", headers=world, params={"program_code": "hypertension"})
    assert resp.status_code == 200, resp.text
    # 修前：全县 3 次（乙院的两次高危也在内）
    assert resp.json()["assessments"] == {"total": 1, "by_risk": {"low": 1}}
