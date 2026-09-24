"""按病历号读就诊病历要判患者可见性并留痕（P0-36）。

`GET /api/quality/records/{record_id}` 原先连调用方身份都不收：任一登录账号按病历号就能读到别家患者
病历的主诉、现病史、既往史、体格检查、诊断依据与治疗方案，且不留痕——与 P0-19 / P0-20 按住院号读
医嘱、按申请号读术中记录同一个形状。2026-09-24 逐条判 P1-69「隔一跳无身份读接口」名单时实测：
与患者毫无关系的乙院医生 200，病历原文全在回执里。同文件的病历清单早就按机构收口，复评也早按病历
所属机构判了归属（ADR-0021），唯独详情什么都不看。

照 P0-19 / P0-20 口径：按病历所属就诊的患者判可见性并留痕（`resource="medical_record"`）。
"""
import pytest

from app.database import SessionLocal
from app.models import AccessLog


def _login(client, username, password="pw123456"):
    token = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def mr_world(client):
    """甲院医生给一位患者建了就诊与病历；乙院医生与这位患者毫无关系。"""
    admin = _login(client, "admin", "admin123")
    orgs = {}
    for key, name in (("a", "病历详情甲院"), ("b", "病历详情乙院")):
        orgs[key] = client.post("/api/organizations",
                                json={"name": name, "org_type": "township", "level": "township"},
                                headers=admin).json()["id"]
        r = client.post("/api/users",
                        json={"username": f"p036_doc_{key}", "password": "pw123456", "full_name": f"{name}医生",
                              "role": "doctor", "org_id": orgs[key]},
                        headers=admin)
        assert r.status_code == 201, r.text
    heads = {k: _login(client, f"p036_doc_{k}") for k in orgs}
    patient = client.post("/api/patients", json={"name": "病历详情患者", "id_card": "320000199204046671"},
                          headers=admin).json()
    enc = client.post("/api/encounters",
                      json={"patient_id": patient["id"], "org_id": orgs["a"], "encounter_type": "outpatient",
                            "diagnosis_name": "胃溃疡"},
                      headers=heads["a"]).json()
    rec = client.post("/api/quality/records",
                      json={"encounter_id": enc["id"], "chief_complaint": "上腹痛三天",
                            "present_illness": "三天前进食后出现上腹部烧灼样疼痛", "past_history": "既往胃溃疡病史",
                            "physical_exam": "体温36.8℃，上腹部压痛", "diagnosis_basis": "病史与体征支持胃溃疡",
                            "treatment_plan": "奥美拉唑口服"},
                      headers=heads["a"])
    assert rec.status_code == 201, rec.text
    return {"h": heads, "admin": admin, "patient_id": patient["id"], "record_id": rec.json()["record"]["id"]}


def _logs(patient_id: int) -> int:
    db = SessionLocal()
    try:
        return db.query(AccessLog).filter(
            AccessLog.patient_id == patient_id, AccessLog.resource == "medical_record"
        ).count()
    finally:
        db.close()


def test_无关机构按病历号读不到病历原文(client, mr_world):
    r = client.get(f"/api/quality/records/{mr_world['record_id']}", headers=mr_world["h"]["b"])
    assert r.status_code == 403, r.text
    assert "上腹痛" not in r.text


def test_有关系照常读且留痕(client, mr_world):
    before = _logs(mr_world["patient_id"])
    r = client.get(f"/api/quality/records/{mr_world['record_id']}", headers=mr_world["h"]["a"])
    assert r.status_code == 200, r.text
    assert r.json()["record"]["chief_complaint"] == "上腹痛三天"
    assert _logs(mr_world["patient_id"]) == before + 1


def test_全域角色照常读也留痕(client, mr_world):
    before = _logs(mr_world["patient_id"])
    assert client.get(f"/api/quality/records/{mr_world['record_id']}", headers=mr_world["admin"]).status_code == 200
    assert _logs(mr_world["patient_id"]) == before + 1


def test_病历号不存在照旧404(client, mr_world):
    r = client.get("/api/quality/records/987654", headers=mr_world["h"]["b"])
    assert r.status_code == 404, r.text
