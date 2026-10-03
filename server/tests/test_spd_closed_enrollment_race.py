"""只挂在管档案的新工作与登记死亡同时到：死者名下挂上运行中的路径、任务和高危复诊（P2-1179，第三十四批扫描 L1-4）。

P2-226 定了「只挂在管档案」：启动路径、手工建任务（带档案号）、绑服务包对非在管档案 409；按病种隐式挂档案、异常监测派
处置任务、高危评估自动开干预与复诊只挂在管的那份（顺序版见 `test_spd_care_closed_enrollment` /
`test_spd_task_closed_enrollment`）。判在管却都是锁外读的：

- 启动路径拿着档案行锁（P2-269 为查重加的），锁里只查重、不复核状态；
- 高危评估锁外取在管档案，进 `_auto_intervene` 的档案行锁开干预与复诊，锁里同样不复核；
- 手工建任务、绑服务包、异常监测派任务连锁都没进。

读到在管之后别人登记死亡并提交（结案收尾 `close_open_work` 已经跑过），这一路照旧挂上去，之后再没人收。扫描实测：
启动路径 201、死亡档案上一条运行中的路径和一条待办任务；高危评估 201、死亡档案的风险分层被改成高危、开出一条高危复诊。

修法：在 `serialized_on(db, SpdEnrollment, …)` 临界区里按列复判在管（`service.enrollment_still_active`，照
`billing.create_bill_detail` 锁里复判在院的写法，不 refresh），没进锁的入口一并进锁；复判不在管的与顺序发生时一样——
同一句 409，或不派发（任务照建、不挂档案）。

时序钉法照 `test_spd_lifecycle_death_race`：锁外判过在管之后、进锁之前，让另一路把档案登记成死亡并提交。
"""
import pytest

from app.database import SessionLocal

B = "/api/spd"

#: 综合风险量表（种子 `assess_risk_common`）答成高危：4+4+3 = 11 → high，落进高危自动干预区间
HIGH_ANSWERS = {"control": "未达标", "adherence": "差", "complication": "1项"}


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21179 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    doctor = client.post("/api/users", headers=admin, json={
        "username": "p21179_doc", "password": "pw123456", "full_name": "P21179 医生", "role": "doctor", "org_id": org})
    assert doctor.status_code == 201, doctor.text
    template = client.post(f"{B}/intervention-templates", headers=admin, json={
        "code": "p21179_auto_high", "name": "P21179 高危自动干预", "program_code": "hypertension", "category": "drug",
        "content": "调整用药", "measures": "两周内复查血压", "frequency": "每周一次", "auto_risk_level": "high"})
    assert template.status_code == 201, template.text
    from app.spd.models import SpdProgram

    with SessionLocal() as db:
        program = db.query(SpdProgram).filter(SpdProgram.code == "hypertension").one().id
    path = client.post(f"{B}/path-templates", headers=admin, json={
        "program_id": program, "code": "P21179_PATH", "name": "P21179 规范管理路径"})
    assert path.status_code == 201, path.text
    node = client.post(f"{B}/path-templates/{path.json()['id']}/nodes", headers=admin, json={
        "key": "n1", "name": "首次随访", "seq": 1, "due_days": 7})
    assert node.status_code == 201, node.text
    published = client.post(f"{B}/path-templates/{path.json()['id']}/status", headers=admin, json={"status": "published"})
    assert published.status_code == 200, published.text
    package = client.post(f"{B}/service-packages", headers=admin, json={
        "code": "p21179_pkg", "name": "P21179 服务包", "program_code": "hypertension", "price": 100, "period_days": 365,
        "items": [{"code": "bp_check", "name": "血压测量", "times": 2, "price": 5}]})
    assert package.status_code == 201, package.text
    return {"org": org, "doctor": doctor.json()["id"], "path": path.json()["id"], "package": package.json()["id"], "n": 0}


def _enrolled(client, admin, world):
    """现造一位患者 + 一份在管高血压档案（风险分层低危），返回 (patient_id, enrollment_id)。"""
    world["n"] += 1
    patient = client.post("/api/patients", headers=admin, json={
        "name": f"P21179 患者{world['n']}", "id_card": f"33012719720303{world['n']:04d}"})
    assert patient.status_code == 201, patient.text
    enrollment = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient.json()["id"], "program_code": "hypertension", "org_id": world["org"], "risk_level": "low"})
    assert enrollment.status_code == 201, enrollment.text
    return patient.json()["id"], enrollment.json()["id"]


def _dies_meanwhile(monkeypatch, module, name, enrollment_id):
    """`module.<name>` 调完之后（锁外判过在管、进锁之前），另一路把档案登记成死亡并提交。"""
    from app.spd.models import SpdEnrollment

    real, fired = getattr(module, name), []

    def racing(*args, **kwargs):
        result = real(*args, **kwargs)
        if not fired:
            fired.append(True)
            with SessionLocal() as other:
                other.get(SpdEnrollment, enrollment_id).status = "dead"
                other.commit()
        return result

    monkeypatch.setattr(module, name, racing)
    return fired


def _rows(model, **filters):
    with SessionLocal() as db:
        return db.query(model).filter_by(**filters).all()


def _enrollment(enrollment_id):
    from app.spd.models import SpdEnrollment

    with SessionLocal() as db:
        row = db.get(SpdEnrollment, enrollment_id)
        return row.status, row.risk_level


def test_启动路径与登记死亡并发_409_死者名下不挂路径和任务(client, admin, world, monkeypatch):
    from app.spd.models import SpdPathInstance, SpdTask
    from app.spd.routers import tasks

    _, enrollment = _enrolled(client, admin, world)
    fired = _dies_meanwhile(monkeypatch, tasks, "_template_node_keys", enrollment)
    resp = client.post(f"{B}/path-instances", headers=admin, json={"enrollment_id": enrollment, "template_id": world["path"]})
    monkeypatch.undo()
    assert fired
    assert resp.status_code == 409, resp.text   # 修前 201：一条运行中的路径
    assert resp.json() == {"detail": "非在管患者不能启动路径"}   # 与顺序发生时同一句
    assert _rows(SpdPathInstance, enrollment_id=enrollment) == []
    assert _rows(SpdTask, enrollment_id=enrollment) == []   # 修前一条「首次随访」待办，没人收
    assert _enrollment(enrollment)[0] == "dead"


def test_高危评估与登记死亡并发_评估照存_不开干预和复诊_风险分层不回写(client, admin, world, monkeypatch):
    from app.spd.models import SpdAssessment, SpdIntervention, SpdRevisit
    from app.spd.routers import care

    patient, enrollment = _enrolled(client, admin, world)
    fired = _dies_meanwhile(monkeypatch, care, "_managed_enrollment_of", enrollment)
    resp = client.post(f"{B}/assessments", headers=admin, json={
        "patient_id": patient, "scale_code": "assess_risk_common", "program_code": "hypertension",
        "answers": HIGH_ANSWERS})
    monkeypatch.undo()
    assert fired
    assert resp.status_code == 201 and resp.json()["risk_level"] == "high", resp.text
    assert len(_rows(SpdAssessment, patient_id=patient)) == 1   # 评估记录照存
    assert _rows(SpdIntervention, patient_id=patient) == []    # 修前一条「高危自动干预」
    assert _rows(SpdRevisit, patient_id=patient) == []         # 修前一条「高危复诊评估」
    assert _enrollment(enrollment) == ("dead", "low")          # 修前风险分层被改成 high


def test_带档案号手工建任务与登记死亡并发_409_不挂任务(client, admin, world, monkeypatch):
    from app.spd.models import SpdTask
    from app.spd.routers import tasks

    patient, enrollment = _enrolled(client, admin, world)
    fired = _dies_meanwhile(monkeypatch, tasks, "unusable_user", enrollment)
    resp = client.post(f"{B}/tasks", headers=admin, json={
        "patient_id": patient, "title": "P21179 手工随访", "enrollment_id": enrollment,
        "assignee_id": world["doctor"], "org_id": world["org"]})
    monkeypatch.undo()
    assert fired
    assert resp.status_code == 409, resp.text   # 修前 201
    assert resp.json() == {"detail": "非在管状态的档案不可新建任务，请先恢复管理"}
    assert _rows(SpdTask, patient_id=patient) == []


def test_按病种手工建任务与登记死亡并发_任务照建但不挂死者的档案(client, admin, world, monkeypatch):
    from app.spd.models import SpdTask
    from app.spd.routers import tasks

    patient, enrollment = _enrolled(client, admin, world)
    fired = _dies_meanwhile(monkeypatch, tasks, "enrollment_for", enrollment)
    resp = client.post(f"{B}/tasks", headers=admin, json={
        "patient_id": patient, "title": "P21179 按病种建", "program_code": "hypertension", "org_id": world["org"]})
    monkeypatch.undo()
    assert fired
    assert resp.status_code == 201, resp.text
    assert resp.json()["enrollment_id"] is None   # 修前挂在死亡档案上；顺序发生时同样不挂
    assert _rows(SpdTask, enrollment_id=enrollment) == []


def test_绑服务包与登记死亡并发_409_不给死者签包(client, admin, world, monkeypatch):
    from app.spd.models import SpdPackageBinding
    from app.spd.routers import population

    _, enrollment = _enrolled(client, admin, world)
    # 档案已经读进会话：之后判在管读的是这份旧对象（与锁外判完、别人才提交同一个效果）
    fired = _dies_meanwhile(monkeypatch, population, "assert_org_writable", enrollment)
    resp = client.post(f"{B}/enrollments/{enrollment}/packages", headers=admin, json={"package_id": world["package"]})
    monkeypatch.undo()
    assert fired
    assert resp.status_code == 409, resp.text   # 修前 201
    assert resp.json() == {"detail": "非在管状态的档案不可绑定服务包，请先恢复管理"}
    assert _rows(SpdPackageBinding, enrollment_id=enrollment) == []


def test_异常监测值与登记死亡并发_监测值照存_不派处置任务(client, admin, world, monkeypatch):
    """修后在第一次写库之前进锁，死亡落在锁外取档案之后、进锁之前，锁里复判不在管、不派。

    修前这一路在 SQLite 上其实撞不出来：监测值先 flush、库级写锁挡住了另一路的死亡（插进去的死亡等满 5 秒报
    database is locked），竞态只在 PG 上（INSERT 不锁档案行）。这条钉的是修后的时序与锁里那次复判。"""
    from app.spd.models import SpdMeasurement, SpdTask
    from app.spd.routers import care

    patient, enrollment = _enrolled(client, admin, world)
    fired = _dies_meanwhile(monkeypatch, care, "_managed_enrollment_of", enrollment)
    resp = client.post(f"{B}/measurements", headers=admin, json={
        "patient_id": patient, "program_code": "hypertension", "metric": "bp_sys", "value": 190, "unit": "mmHg"})
    monkeypatch.undo()
    assert fired
    assert resp.status_code == 201 and resp.json()["level"] == "high", resp.text
    assert len(_rows(SpdMeasurement, patient_id=patient)) == 1
    assert _rows(SpdTask, patient_id=patient) == []


def test_没有竞争时照常启动路径_派发与签包(client, admin, world):
    from app.spd.models import SpdIntervention, SpdRevisit, SpdTask

    patient, enrollment = _enrolled(client, admin, world)
    started = client.post(f"{B}/path-instances", headers=admin, json={"enrollment_id": enrollment, "template_id": world["path"]})
    assert started.status_code == 201 and started.json()["status"] == "running", started.text
    assessed = client.post(f"{B}/assessments", headers=admin, json={
        "patient_id": patient, "scale_code": "assess_risk_common", "program_code": "hypertension",
        "answers": HIGH_ANSWERS})
    assert assessed.status_code == 201, assessed.text
    assert [i.goal for i in _rows(SpdIntervention, enrollment_id=enrollment)] == ["高危自动干预"]
    assert [r.items for r in _rows(SpdRevisit, patient_id=patient)] == ["高危复诊评估"]
    assert _enrollment(enrollment) == ("active", "high")
    measured = client.post(f"{B}/measurements", headers=admin, json={
        "patient_id": patient, "program_code": "hypertension", "metric": "bp_sys", "value": 190, "unit": "mmHg"})
    assert measured.status_code == 201, measured.text
    created = client.post(f"{B}/tasks", headers=admin, json={
        "patient_id": patient, "title": "P21179 照常", "enrollment_id": enrollment,
        "assignee_id": world["doctor"], "org_id": world["org"]})
    assert created.status_code == 201 and created.json()["enrollment_id"] == enrollment, created.text
    titles = sorted(t.title for t in _rows(SpdTask, enrollment_id=enrollment))
    assert titles == sorted(["P21179 规范管理路径·首次随访", "指标异常处置：bp_sys 190.0mmHg", "P21179 照常"])
    bound = client.post(f"{B}/enrollments/{enrollment}/packages", headers=admin, json={"package_id": world["package"]})
    assert bound.status_code == 201, bound.text
