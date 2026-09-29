"""病案首页与 DRG 入组同一次提交：入组那一步出错，首页不落库，改好后重新提交照常入组（P2-822，第二十二批「失败路径的
半截状态」扫描 X1-6）。

`create_case_summary` 先经 `insert_or_conflict` 提交首页，再调 `assign_drg_group` 回填 drg_code 并第二次提交。入组那一步
出错（库抖动、连接断）→ 500，首页已落库、drg_code 空、权重 0；首页只能新建（P2-182），重新提交 409「病案首页已填写」——
这份首页永不入组，DRG 统计按例数计入却不入组，入组率被永久拉低。修后先入组（只赋值不提交）、再插首页，一次提交。
"""
import pytest
from sqlalchemy.exc import OperationalError

import app.routers.drgs as drgs
from app.database import SessionLocal
from app.models import CaseSummary

BODY = {"discharge_diagnosis": "社区获得性肺炎", "total_cost": 6000, "outcome": "好转"}


@pytest.fixture(scope="module")
def admission(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2822 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P2822 呼吸科"}).json()["id"]
    bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward, "bed_no": "P2822-1"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2822 患者", "id_card": "330106196001012822"}).json()["id"]
    adm = client.post("/api/inpatient/admissions", headers=admin, json={
        "patient_id": patient, "ward_id": ward, "bed_id": bed, "diagnosis_name": "社区获得性肺炎"})
    assert adm.status_code == 201, adm.text
    return adm.json()["id"]


def _summaries(admission_id):
    with SessionLocal() as db:
        return [(s.drg_code, s.drg_weight) for s in db.query(CaseSummary).filter(CaseSummary.admission_id == admission_id)]


def test_入组出错_首页不落库_重新提交照常入组(client, admin, admission, monkeypatch):
    def boom(db, summary):
        raise OperationalError("SELECT drg_groups ...", {}, Exception("server closed the connection unexpectedly"))

    monkeypatch.setattr(drgs, "assign_drg_group", boom)
    with pytest.raises(OperationalError):
        client.post(f"/api/inpatient/admissions/{admission}/case-summary", headers=admin, json=BODY)
    assert _summaries(admission) == []   # 修前：首页已落库，drg_code 空、权重 0
    monkeypatch.undo()

    resp = client.post(f"/api/inpatient/admissions/{admission}/case-summary", headers=admin, json=BODY)
    assert resp.status_code == 201, resp.text   # 修前 409「病案首页已填写」，永不入组
    body = resp.json()
    assert body["drg_code"] and body["drg"]["drg_code"] == body["drg_code"]
    assert _summaries(admission) == [(body["drg_code"], body["drg_weight"])]   # 入组结果与首页同一次落库


def test_入组不再自己提交():
    import inspect

    assert "db.commit()" not in inspect.getsource(drgs.assign_drg_group)
