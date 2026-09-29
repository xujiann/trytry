"""团队工作台按（患者, 病种）属于我的在管档案关联，待建路径按档案本身判（P2-851，第二十三批「同一患者名下多份并存」扫描 Y3-3）。

同一患者：高血压归东镇甲医生、糖尿病归西镇乙医生。团队工作台（成员 / 个案管理师 / 专家端）原先按「患者」关联：

- 待建路径看患者名下**任何**档案有没有**任何状态**的路径——甲给高血压启动了路径，乙的「待建路径」1→0（糖尿病档案一条
  路径都没有）；已取消的也算有路径；
- 待评估看患者名下有没有任何评估——带 `program_code=diabetes` 也一样；
- 到期随访 / 到期复诊 / 指标异常 / 在途转诊只看 `patient_id IN 我的患者`——乙排的复诊、录的偏高血糖、今天到期的糖尿病
  随访，都进了甲的数（甲本人复诊清单是 0，去执行那条随访 403）。

修后这四类按「同一患者、同一病种（没写病种的照旧算）、随访再看机构」属于我在管的档案；待建路径按这份档案自己有没有
进行中 / 已完成的路径；待评估带了病种按（患者, 病种）判，不带照旧按人（P2-139）。单病种患者的数字不变。
"""
import pytest

from app import clock
from app.database import SessionLocal

B = "/api/spd"


def _login(client, username):
    token = client.post("/api/auth/login", json={"username": username, "password": "passw0rd1"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def world(client, admin):
    from app.spd.models import SpdFollowupRecord, SpdPathInstance, SpdPathTemplate, SpdProgram

    orgs, doctors, headers = {}, {}, {}
    for key in ("east", "west"):
        orgs[key] = client.post("/api/organizations", headers=admin, json={
            "name": f"P2851 {key}卫生院", "org_type": "township", "level": "township"}).json()["id"]
        made = client.post("/api/users", headers=admin, json={
            "username": f"p2851_{key}", "password": "passw0rd1", "full_name": f"p2851_{key}", "role": "doctor",
            "org_id": orgs[key]})
        assert made.status_code in (200, 201), made.text
        doctors[key] = made.json()["id"]
        headers[key] = _login(client, f"p2851_{key}")
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2851 张三", "id_card": "330102195001012851"}).json()["id"]
    lone = client.post("/api/patients", headers=admin, json={
        "name": "P2851 路径已取消", "id_card": "330102195001022851"}).json()["id"]
    enrollments = {}
    for key, pid, program, org in (("htn", patient, "hypertension", "east"), ("dm", patient, "diabetes", "west"),
                                   ("lone", lone, "hypertension", "east")):
        made = client.post(f"{B}/enrollments", headers=admin, json={
            "patient_id": pid, "program_code": program, "org_id": orgs[org], "doctor_user_id": doctors[org]})
        assert made.status_code == 201, made.text
        enrollments[key] = made.json()["id"]
    today = clock.today().isoformat()
    with SessionLocal() as db:
        program_id = db.query(SpdProgram.id).filter(SpdProgram.code == "hypertension").scalar()
        template = SpdPathTemplate(program_id=program_id, code="P2851_T", name="P2851 路径")
        db.add(template)
        db.flush()
        db.add_all([
            SpdPathInstance(enrollment_id=enrollments["htn"], template_id=template.id, status="running"),
            SpdPathInstance(enrollment_id=enrollments["lone"], template_id=template.id, status="cancelled"),
            # 西镇排的糖尿病随访，今天到期
            SpdFollowupRecord(patient_id=patient, program_code="diabetes", org_id=orgs["west"], planned_at=today),
        ])
        db.commit()
    assessed = client.post(f"{B}/assessments", headers=headers["east"], json={
        "patient_id": patient, "scale_code": "scr_hypertension", "program_code": "hypertension",
        "answers": {"family": "是"}})
    assert assessed.status_code in (200, 201), assessed.text   # 只做了高血压评估
    revisit = client.post(f"{B}/revisits", headers=headers["west"], json={
        "patient_id": patient, "program_code": "diabetes", "plan_date": today, "doctor_user_id": doctors["west"]})
    assert revisit.status_code == 201, revisit.text
    measured = client.post(f"{B}/measurements", headers=headers["west"], json={
        "patient_id": patient, "program_code": "diabetes", "metric": "glucose_fasting", "value": 15.2,
        "unit": "mmol/L"})
    assert measured.status_code in (200, 201) and measured.json()["level"] == "high", measured.text
    return {"headers": headers}


def _board(client, headers, **params):
    resp = client.get(f"{B}/workbench/team", headers=headers, params={"role": "member", **params})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return {**body["plans"], **body["alerts"]}


def test_别的病种别家机构的随访复诊读数_不算进我的(client, world):
    east = _board(client, world["headers"]["east"])
    assert (east["due_followups"], east["due_revisits"], east["abnormal_measure"]) == (0, 0, 0), east   # 修前 (1, 1, 1)
    west = _board(client, world["headers"]["west"])
    assert (west["due_followups"], west["due_revisits"], west["abnormal_measure"]) == (1, 1, 1), west


def test_待建路径按档案本身_取消的不算有路径(client, world):
    assert _board(client, world["headers"]["west"])["pending_path"] == 1   # 修前 0：患者的高血压档案有路径
    assert _board(client, world["headers"]["east"])["pending_path"] == 1   # 修前 0：只有一条已取消的路径


def test_待评估带病种按病种判_不带照旧按人(client, world):
    west = world["headers"]["west"]
    assert _board(client, west, program_code="diabetes")["pending_assess"] == 1   # 修前 0：做的是高血压评估
    assert _board(client, west)["pending_assess"] == 0   # 不带病种：按人，这位患者评估过
