"""居民自报的监测值判级取老档案的阶段：同一个值，居民自报与医生录入判出两个等级（P2-227）。

判级按「病种 + 阶段 + 指标」取管理目标，阶段取自这位患者这个病种的纳管档案。管理端录入取「在管的优先」
（`care._enrollment_of` / `service.enrollment_for`）；居民端自报（`POST /api/portal/spd/measurements`）原先随手取
第一份——迁出 / 排除后重新建档的患者，取到的是老档案停在那一天的阶段。老阶段的目标更严（或更松）时，居民在家
量的 150 判「偏高」、医生诊室录的 150 判「正常」，异常清单与首页提示对不上。

修法：居民端与管理端同一句（`enrollment_for`，在管的优先）。
"""
import pytest

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    from app.database import SessionLocal
    from app.spd.models import SpdEnrollment, SpdProgram, SpdTarget

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2227 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2227 居民", "id_card": "330127197101012227", "phone": "13922272227"}).json()
    with SessionLocal() as db:
        program = db.query(SpdProgram).filter_by(code="hypertension").one()
        # 两个阶段的收缩压目标：老阶段上限 130、新阶段上限 160（阶段名本文件独有，不影响别的用例取目标）
        db.add_all([
            SpdTarget(program_id=program.id, stage="p2227_old", metric="bp_sys", target_low=90, target_high=130,
                      unit="mmHg", active=True),
            SpdTarget(program_id=program.id, stage="p2227_new", metric="bp_sys", target_low=90, target_high=160,
                      unit="mmHg", active=True),
        ])
        # 先有一份排除了的老档案（停在老阶段），再重新建档在管（新阶段）
        db.add(SpdEnrollment(patient_id=patient["id"], program_code="hypertension", org_id=org,
                             stage="p2227_old", status="excluded"))
        db.flush()
        db.add(SpdEnrollment(patient_id=patient["id"], program_code="hypertension", org_id=org,
                             stage="p2227_new", status="active"))
        db.commit()
    return patient


def _resident_headers(client, patient):
    code = client.post("/api/portal/auth/sms/code", json={"phone": patient["phone"]}).json()["debug_code"]
    login = client.post("/api/portal/auth/sms/login", json={"phone": patient["phone"], "code": code})
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    bound = client.post("/api/portal/auth/realname", headers=headers, json={
        "name": patient["name"], "id_card": patient["id_card"]})
    assert bound.status_code == 200 or bound.json() == {"detail": "该账户已完成实名绑定"}, bound.text
    return headers


def test_同一个值_居民自报与医生录入按在管档案的阶段判出同一个等级(client, admin, world):
    doctor = client.post(f"{B}/measurements", headers=admin, json={
        "patient_id": world["id"], "program_code": "hypertension", "metric": "bp_sys", "value": 150, "unit": "mmHg"})
    assert doctor.status_code == 201 and doctor.json()["level"] == "normal", doctor.text   # 在管档案的新阶段：上限 160

    resident = client.post("/api/portal/spd/measurements", headers=_resident_headers(client, world), json={
        "program_code": "hypertension", "metric": "bp_sys", "value": 150, "unit": "mmHg"})
    assert resident.status_code == 201, resident.text
    assert resident.json()["level"] == "normal"   # 修前 high：取了排除掉的老档案的老阶段（上限 130）
