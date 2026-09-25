"""管理端上报异常不带病种：上报挂不上纳管档案，村医的「异常上报」积分从界面上一分都拿不到，派生的处置任务也不挂档案（P2-100）。

上报（`POST /case-reports`）按「患者 + 病种」取纳管档案：取到了才给档案上的村医记「异常上报」积分（种子规则 5 分）、派生的
处置任务才挂到档案上。可管理端的上报表单只有患者、上报任务、类型、说明——不带病种；上报任务本身是配了病种的（上报任务
配置「病种、管理科室与负责人」），接口却不看它。于是从界面上报的每一条病种都是空串：取不到档案、不给积分、任务不挂档案，
按病种统计的上报也数不到它。村医考核里「异常上报」这一项的积分因此只有接口调用方拿得到。

修法：没写病种的，取上报任务的病种；写了的，须与上报任务的病种一致（与 P2-98 / P2-99 同一口径，不一致 422）。
上报表单补一个病种下拉（留空 = 取上报任务的）。
"""
import pytest

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2100 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    vd = client.post("/api/users", headers=admin, json={
        "username": "p2100_vd", "password": "pw123456", "full_name": "P2100 村医", "role": "doctor", "org_id": org})
    assert vd.status_code == 201, vd.text
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2100 患者", "id_card": "330127196404042100"}).json()["id"]
    enrollment = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient, "program_code": "hypertension", "org_id": org,
        "village_doctor_id": vd.json()["id"]})
    assert enrollment.status_code == 201, enrollment.text
    tasks = {}
    for program in ("hypertension", "diabetes", ""):
        t = client.post(f"{B}/case-report-tasks", headers=admin, json={
            "code": f"P2100_{program or 'any'}", "name": f"P2100 {program or '通用'} 上报", "program_code": program})
        assert t.status_code == 201, t.text
        tasks[program] = t.json()["id"]
    return {"patient": patient, "enrollment": enrollment.json()["id"], "vd": vd.json()["id"], "tasks": tasks}


def _points_for(report_id):
    from app.database import SessionLocal
    from app.spd.models import SpdPointRecord

    with SessionLocal() as db:
        return db.query(SpdPointRecord).filter_by(ref_type="case_report", ref_id=report_id).count()


def _spawned(patient_id):
    from app.database import SessionLocal
    from app.spd.models import SpdTask

    with SessionLocal() as db:
        task = db.query(SpdTask).filter_by(patient_id=patient_id, source="report").order_by(SpdTask.id.desc()).first()
        return task.program_code, task.enrollment_id


def test_界面那样不带病种上报_取上报任务的病种_积分与档案都挂上(client, admin, world):
    resp = client.post(f"{B}/case-reports", headers=admin, json={
        "patient_id": world["patient"], "task_id": world["tasks"]["hypertension"], "report_type": "review",
        "content": "P2100 血压 190/110"})
    assert resp.status_code == 201, resp.text
    assert _points_for(resp.json()["id"]) == 1   # 修前 0：取不到档案，村医的异常上报积分不入账
    assert _spawned(world["patient"]) == ("hypertension", world["enrollment"])   # 修前 ("", None)


def test_写的病种与上报任务的对不上_422(client, admin, world):
    resp = client.post(f"{B}/case-reports", headers=admin, json={
        "patient_id": world["patient"], "task_id": world["tasks"]["diabetes"], "program_code": "hypertension",
        "report_type": "review", "content": "P2100 对不上"})
    assert resp.status_code == 422, resp.text   # 修前 201
    assert resp.json() == {"detail": "上报任务的病种与上报病种不一致"}


def test_通用上报任务不带病种_照旧不猜(client, admin, world):
    resp = client.post(f"{B}/case-reports", headers=admin, json={
        "patient_id": world["patient"], "task_id": world["tasks"][""], "report_type": "review",
        "content": "P2100 通用"})
    assert resp.status_code == 201, resp.text
    assert _points_for(resp.json()["id"]) == 0   # 病种不明，不替人猜是哪份档案
