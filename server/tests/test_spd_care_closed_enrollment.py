"""已登记死亡 / 排除 / 迁出 / 召回的纳管档案不再挂新的工作（P2-226）。

`care._enrollment_of` 取「该病种的档案，在管的优先」，没有在管的就退到最近一份历史档案。拿来给监测值判级取阶段没问题，
可五个写入口拿它往档案上挂新的工作——2026-09-26 开发库实测（修前代码），高血压档案登记死亡之后：

- 录一条收缩压 190：派一条「指标异常处置」任务挂在死亡档案上、派给原主管医生，三天后超期；
- 做一次极高危评估：死亡档案的风险分层被改成极高危（改档接口对非在管档案一律 409），高危复诊照开；
- 个案上报：处置任务挂在死亡档案上，原村医照记 5 分「异常上报」；
- 咨询转随访、手工下发干预：随访任务、干预与它派的执行任务都挂在死亡档案上。

结案时 `close_open_work` 已经收过尾，之后挂上去的工作再没人收——P1-129 修掉的「死者名下的随访照旧超期」换了个入口。

修法：派任务、回写风险分层、高危自动干预、记积分只挂**在管**档案（`_managed_enrollment_of`），与改档 / 绑服务包 /
启动路径对非在管档案 409、手工建任务选了病种「只挂在管的」同一句。没有在管档案的走「没入组」那条路：监测、评估、
上报记录照存（判级照旧取这份档案的阶段），不往档案上派生工作。在管档案的照旧（对照组）。
"""
import pytest

B = "/api/spd"

#: 综合风险量表（种子 `assess_risk_common`）的满分答法：4+4+6+4=18 ≥ 14 → very_high，落进高危自动干预区间
VERY_HIGH_ANSWERS = {"control": "未达标", "adherence": "差", "complication": "2项及以上", "selfcare": "完全依赖"}


def _enroll(client, admin, org, vd, name, id_card):
    patient = client.post("/api/patients", headers=admin, json={"name": name, "id_card": id_card})
    assert patient.status_code == 201, patient.text
    enrollment = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient.json()["id"], "program_code": "hypertension", "org_id": org,
        "risk_level": "low", "village_doctor_id": vd})
    assert enrollment.status_code == 201, enrollment.text
    return patient.json()["id"], enrollment.json()["id"]


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2226 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    vd = client.post("/api/users", headers=admin, json={
        "username": "p2226_vd", "password": "pw123456", "full_name": "P2226 村医", "role": "doctor", "org_id": org})
    assert vd.status_code == 201, vd.text
    dead, dead_enrollment = _enroll(client, admin, org, vd.json()["id"], "P2226 已故", "330127197011112226")
    alive, alive_enrollment = _enroll(client, admin, org, vd.json()["id"], "P2226 在管", "330127197012122226")
    resp = client.post(f"{B}/enrollments/{dead_enrollment}/lifecycle", headers=admin,
                       json={"event": "death", "reason": "病故"})
    assert resp.status_code == 200 and resp.json()["enrollment"]["status"] == "dead", resp.text
    return {"dead": dead, "dead_enrollment": dead_enrollment, "alive": alive, "alive_enrollment": alive_enrollment}


def _db():
    from app.database import SessionLocal

    return SessionLocal()


def _tasks(patient_id, **filters):
    """这位患者的任务：[(标题, 挂的档案)]，按建立先后。"""
    from app.spd.models import SpdTask

    with _db() as db:
        rows = db.query(SpdTask).filter_by(patient_id=patient_id, **filters).order_by(SpdTask.id).all()
        return [(t.title, t.enrollment_id) for t in rows]


def test_死亡档案_异常监测值照判级但不派处置任务(client, admin, world):
    resp = client.post(f"{B}/measurements", headers=admin, json={
        "patient_id": world["dead"], "program_code": "hypertension", "metric": "bp_sys", "value": 190, "unit": "mmHg"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["level"] == "high"   # 判级照旧（取这份档案的阶段比管理目标）
    # 修前：一条「指标异常处置」挂在死亡档案上、派给原主管医生，三天后超期
    assert [t for t in _tasks(world["dead"]) if t[0].startswith("指标异常处置")] == []


def test_死亡档案_评估照存但不改档案的风险分层_不开高危复诊与干预(client, admin, world):
    from app.spd.models import SpdEnrollment, SpdIntervention, SpdRevisit

    resp = client.post(f"{B}/assessments", headers=admin, json={
        "patient_id": world["dead"], "scale_code": "assess_risk_common", "program_code": "hypertension",
        "answers": VERY_HIGH_ANSWERS})
    assert resp.status_code == 201 and resp.json()["risk_level"] == "very_high", resp.text
    with _db() as db:
        assert db.get(SpdEnrollment, world["dead_enrollment"]).risk_level == "low"   # 修前 very_high
        assert db.query(SpdRevisit).filter_by(patient_id=world["dead"]).count() == 0   # 修前 1 条高危复诊
        assert db.query(SpdIntervention).filter_by(enrollment_id=world["dead_enrollment"]).count() == 0


def test_死亡档案_个案上报照收_处置任务不挂档案_不给原村医记分(client, admin, world):
    from app.spd.models import SpdPointRecord

    resp = client.post(f"{B}/case-reports", headers=admin, json={
        "patient_id": world["dead"], "program_code": "hypertension", "report_type": "review",
        "content": "P2226 家属来电"})
    assert resp.status_code == 201, resp.text
    assert _tasks(world["dead"], source="report")[-1][1] is None   # 修前挂在死亡档案上
    with _db() as db:
        assert db.query(SpdPointRecord).filter_by(ref_type="case_report", ref_id=resp.json()["id"]).count() == 0


def test_死亡档案_咨询转随访与手工下发干预都不挂档案(client, admin, world):
    from app.spd.models import SpdConsult, SpdIntervention

    with _db() as db:
        consult = SpdConsult(patient_id=world["dead"], program_code="hypertension", status="open")
        db.add(consult)
        db.commit()
        consult_id = consult.id
    resp = client.post(f"{B}/consults/{consult_id}/to-followup", headers=admin, json={"title": "P2226 咨询转随访"})
    assert resp.status_code == 200, resp.text
    assert _tasks(world["dead"], title="P2226 咨询转随访") == [("P2226 咨询转随访", None)]   # 修前挂在死亡档案上

    resp = client.post(f"{B}/interventions", headers=admin, json={
        "patient_ids": [world["dead"]], "program_code": "hypertension", "goal": "P2226 限盐",
        "content": "每日盐不超过 5 克", "create_task": True})
    assert resp.status_code == 201, resp.text
    with _db() as db:
        (intervention,) = db.query(SpdIntervention).filter(SpdIntervention.id.in_(resp.json()["ids"])).all()
        assert (intervention.program_code, intervention.enrollment_id) == ("hypertension", None)
    assert _tasks(world["dead"], title="干预执行：P2226 限盐") == [("干预执行：P2226 限盐", None)]


def test_对照_在管档案照旧派任务_回写风险_记分(client, admin, world):
    from app.spd.models import SpdEnrollment, SpdPointRecord, SpdRevisit

    pid, enrollment_id = world["alive"], world["alive_enrollment"]
    measured = client.post(f"{B}/measurements", headers=admin, json={
        "patient_id": pid, "program_code": "hypertension", "metric": "bp_sys", "value": 190, "unit": "mmHg"})
    assert measured.status_code == 201, measured.text
    assert [t for t in _tasks(pid) if t[0].startswith("指标异常处置")] == [("指标异常处置：bp_sys 190.0mmHg", enrollment_id)]

    assessed = client.post(f"{B}/assessments", headers=admin, json={
        "patient_id": pid, "scale_code": "assess_risk_common", "program_code": "hypertension",
        "answers": VERY_HIGH_ANSWERS})
    assert assessed.status_code == 201, assessed.text
    reported = client.post(f"{B}/case-reports", headers=admin, json={
        "patient_id": pid, "program_code": "hypertension", "report_type": "review", "content": "P2226 血压高"})
    assert reported.status_code == 201, reported.text
    assert _tasks(pid, source="report")[-1][1] == enrollment_id
    with _db() as db:
        assert db.get(SpdEnrollment, enrollment_id).risk_level == "very_high"
        assert db.query(SpdRevisit).filter_by(patient_id=pid, source="high_risk").count() == 1
        assert db.query(SpdPointRecord).filter_by(ref_type="case_report", ref_id=reported.json()["id"]).count() == 1
