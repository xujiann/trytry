"""批量下发干预不写病种，干预与它派的执行任务就不挂纳管档案（P1-139 同一族）。

管理端「批量下发干预」表单的病种默认留空（下拉是筛选用的「全部病种」）；`POST /interventions` 按「患者 + 病种」取档案，
病种为空就不挂——干预记录与「干预执行」任务按档案都看不到，引用了病种模板的，干预的病种还是空的（P2-99 的模板 /
病种一致校验也因此被绕过）。修法：没写病种的先随模板（与 P2-100 上报随上报任务同一口径）；再没有的，患者只在管
一个病种的挂这份档案（`service.enrollment_for`）；在管几个病种的不替人猜。
"""
import pytest

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P1139I 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    single = client.post("/api/patients", headers=admin, json={
        "name": "P1139I 单病种", "id_card": "330127196807071139"}).json()["id"]
    multi = client.post("/api/patients", headers=admin, json={
        "name": "P1139I 多病种", "id_card": "330127196808081139"}).json()["id"]
    enrollments = {}
    for patient, program in ((single, "hypertension"), (multi, "hypertension"), (multi, "diabetes")):
        resp = client.post(f"{B}/enrollments", headers=admin, json={
            "patient_id": patient, "program_code": program, "org_id": org})
        assert resp.status_code == 201, resp.text
        enrollments[(patient, program)] = resp.json()["id"]
    template = client.post(f"{B}/intervention-templates", headers=admin, json={
        "code": "P1139I_T", "name": "P1139I 限盐", "program_code": "hypertension", "content": "每日食盐 < 5 克"})
    assert template.status_code == 201, template.text
    return {"single": single, "multi": multi, "enrollments": enrollments, "template": template.json()["id"]}


def _latest(patient_id):
    from app.database import SessionLocal
    from app.spd.models import SpdIntervention, SpdTask

    with SessionLocal() as db:
        record = db.query(SpdIntervention).filter_by(patient_id=patient_id).order_by(SpdIntervention.id.desc()).first()
        task = db.query(SpdTask).filter(SpdTask.patient_id == patient_id,
                                        SpdTask.title.like("干预执行%")).order_by(SpdTask.id.desc()).first()
        return (record.program_code, record.enrollment_id), (task.program_code, task.enrollment_id)


def _issue(client, admin, patient, **extra):
    resp = client.post(f"{B}/interventions", headers=admin, json={
        "patient_ids": [patient], "content": "P1139I 干预", "create_task": True, **extra})
    assert resp.status_code == 201, resp.text


def test_不写病种引用了模板_随模板的病种挂档案(client, admin, world):
    _issue(client, admin, world["multi"], template_id=world["template"])
    expected = ("hypertension", world["enrollments"][(world["multi"], "hypertension")])
    assert _latest(world["multi"]) == (expected, expected)   # 修前 (("", None), ("", None))


def test_不写病种不引用模板_只在管一个病种的挂这份档案(client, admin, world):
    _issue(client, admin, world["single"])
    expected = ("hypertension", world["enrollments"][(world["single"], "hypertension")])
    assert _latest(world["single"]) == (expected, expected)   # 修前 (("", None), ("", None))


def test_在管几个病种又不引用模板的_不替人猜(client, admin, world):
    _issue(client, admin, world["multi"])
    assert _latest(world["multi"]) == (("", None), ("", None))
