"""慢专病评估清单与干预清单按调用方收口（P0-32）。

2026-09-24 用动态探针量「一家与任何患者都没有关系的新机构，调全部清单接口能拿回谁」时撞出：
`GET /api/spd/assessments` 与 `GET /api/spd/interventions` 只在带 `patient_id` 时判可见性，
不带就列出**全域**全部评估（患者姓名、量表得分、风险分级）与干预（患者姓名、干预内容）——
与 P0-23 的咨询清单、P0-24 的复诊看板同一个文件、同一个形状。静态闸门看不出来：
守卫就在函数体里，只是被包在 `if patient_id is not None:` 里。

照 P0-23 / P0-24 口径：走 `scope_patient_list`（全域角色不过滤，其余只见本机构服务过的患者）。
"""
import pytest

from app.database import SessionLocal
from app.spd.models import SpdAssessment, SpdIntervention, SpdScale


def _login(client, username, password="pw123456"):
    token = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def care_world(client):
    """甲院接诊过的患者做过评估、有干预；乙院一名医师与该患者毫无关系。"""
    admin = _login(client, "admin", "admin123")
    a = client.post("/api/organizations",
                    json={"name": "评估干预甲院", "org_type": "township", "level": "township"},
                    headers=admin).json()
    b = client.post("/api/organizations",
                    json={"name": "评估干预乙院", "org_type": "township", "level": "township"},
                    headers=admin).json()
    for uname, org in (("p032_doc_a", a), ("p032_doc_b", b)):
        r = client.post("/api/users",
                        json={"username": uname, "password": "pw123456", "full_name": uname,
                              "role": "doctor", "org_id": org["id"]},
                        headers=admin)
        assert r.status_code == 201, r.text
    doc_a = _login(client, "p032_doc_a")
    patient = client.post("/api/patients",
                          json={"name": "评估干预患者", "id_card": "320000198606066679"},
                          headers=admin).json()
    enc = client.post("/api/encounters",
                      json={"patient_id": patient["id"], "org_id": a["id"], "encounter_type": "outpatient"},
                      headers=doc_a)
    assert enc.status_code == 201, enc.text
    db = SessionLocal()
    try:
        scale = db.query(SpdScale).first()  # 启动种子里的量表：评估必须挂在一张量表上
        assert scale is not None, "没有种子量表，前提变了"
        assess = SpdAssessment(patient_id=patient["id"], scale_id=scale.id, scale_code=scale.code,
                               program_code="hypertension", answers={}, score=17, risk_level="high")
        intv = SpdIntervention(patient_id=patient["id"], program_code="hypertension", goal="血压达标",
                               content="低盐饮食")
        db.add_all([assess, intv])
        db.commit()
        ids = {"assessment": assess.id, "intervention": intv.id}
    finally:
        db.close()
    return {"admin": admin, "doc_a": doc_a, "doc_b": _login(client, "p032_doc_b"),
            "patient_id": patient["id"], "ids": ids}


@pytest.mark.parametrize("kind", ["assessment", "intervention"])
def test_清单不带患者号时按可见患者收口(client, care_world, kind):
    rid = care_world["ids"][kind]

    def ids(headers):
        r = client.get(f"/api/spd/{kind}s?limit=500", headers=headers)
        assert r.status_code == 200, r.text
        return {row["id"] for row in r.json()}

    assert rid not in ids(care_world["doc_b"]), f"乙院看得见甲院患者的{kind}（带姓名）"
    assert rid in ids(care_world["doc_a"])
    assert rid in ids(care_world["admin"])


@pytest.mark.parametrize("kind", ["assessment", "intervention"])
def test_按患者查仍然判定(client, care_world, kind):
    pid = care_world["patient_id"]
    assert client.get(f"/api/spd/{kind}s?patient_id={pid}", headers=care_world["doc_b"]).status_code == 403
    assert client.get(f"/api/spd/{kind}s?patient_id={pid}", headers=care_world["doc_a"]).status_code == 200
