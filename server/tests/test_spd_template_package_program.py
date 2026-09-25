"""干预模板、服务包不看是不是这个病种的：高血压患者下发糖尿病的干预、绑上糖尿病的服务包（P2-99）。

与 P2-95（路径模板）、P2-98（筛查 / 评估量表）同一种缺口：被引用的对象挂在病种上（空串是通用的），引用它的接口只查
在不在、停没停用，不看病种对不对得上。

- 批量下发干预（`POST /interventions`）：病种与模板各选各的，管理端表单的模板下拉又列全部病种的模板——病种选高血压、
  模板点到糖尿病的「控糖饮食」，201，高血压患者的干预内容、措施、频次全是糖尿病的，挂的还是高血压档案。
- 绑服务包（`POST /enrollments/{id}/packages`，以及签约时带 `package_id`）：绑包弹窗列全部病种的服务包——高血压档案
  绑上糖尿病的包，201，居民端多一张别的病种的项目与价目卡片，用量核销按那张包的项目走。

修法：模板 / 服务包须是这个病种的或通用的，不一致 422、什么都不建；管理端的下拉按病种联动。
"""
import pytest

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P299 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P299 患者", "id_card": "330127196510100299"}).json()["id"]
    enrollment = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient, "program_code": "hypertension", "org_id": org})
    assert enrollment.status_code == 201, enrollment.text
    templates, packages = {}, {}
    for program in ("hypertension", "diabetes", ""):
        t = client.post(f"{B}/intervention-templates", headers=admin, json={
            "code": f"P299_T_{program or 'any'}", "name": f"P299 {program or '通用'} 干预",
            "program_code": program, "content": f"{program or '通用'} 的干预内容"})
        assert t.status_code == 201, t.text
        templates[program] = t.json()["id"]
        k = client.post(f"{B}/service-packages", headers=admin, json={
            "code": f"P299_K_{program or 'any'}", "name": f"P299 {program or '通用'} 包", "program_code": program,
            "items": [{"code": "visit", "name": "上门", "times": 2}]})
        assert k.status_code == 201, k.text
        packages[program] = k.json()["id"]
    return {"org": org, "patient": patient, "enrollment": enrollment.json()["id"],
            "templates": templates, "packages": packages}


def _interventions(patient_id):
    from app.database import SessionLocal
    from app.spd.models import SpdIntervention

    with SessionLocal() as db:
        return db.query(SpdIntervention).filter_by(patient_id=patient_id).count()


def test_别的病种的干预模板_下发422_一条不建(client, admin, world):
    resp = client.post(f"{B}/interventions", headers=admin, json={
        "patient_ids": [world["patient"]], "program_code": "hypertension",
        "template_id": world["templates"]["diabetes"]})
    assert resp.status_code == 422, resp.text   # 修前 201：高血压患者的干预内容是糖尿病的
    assert resp.json() == {"detail": "干预模板的病种与干预病种不一致"}
    assert _interventions(world["patient"]) == 0


def test_本病种与通用的干预模板照常下发(client, admin, world):
    for program in ("hypertension", ""):
        resp = client.post(f"{B}/interventions", headers=admin, json={
            "patient_ids": [world["patient"]], "program_code": "hypertension",
            "template_id": world["templates"][program], "create_task": False})
        assert resp.status_code == 201, resp.text


def test_别的病种的服务包_绑定422(client, admin, world):
    url = f"{B}/enrollments/{world['enrollment']}/packages"
    resp = client.post(url, headers=admin, json={"package_id": world["packages"]["diabetes"]})
    assert resp.status_code == 422, resp.text   # 修前 201
    assert resp.json() == {"detail": "服务包的病种与纳管档案不一致"}
    for program in ("hypertension", ""):
        ok = client.post(url, headers=admin, json={"package_id": world["packages"][program]})
        assert ok.status_code == 201, ok.text


def test_签约时带别的病种的服务包_422_档案不建(client, admin, world):
    other = client.post("/api/patients", headers=admin, json={
        "name": "P299 患者乙", "id_card": "330127196510100300"}).json()["id"]
    resp = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": other, "program_code": "hypertension", "org_id": world["org"],
        "package_id": world["packages"]["diabetes"]})
    assert resp.status_code == 422 and resp.json() == {"detail": "服务包的病种与纳管档案不一致"}, resp.text
    rows = client.get(f"{B}/enrollments?limit=500", headers=admin).json()
    assert [r for r in rows if r["patient_id"] == other] == []
