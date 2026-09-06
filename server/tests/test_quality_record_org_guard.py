"""ADR-0021 的回归：结构化病历写入必须校验**就诊所属机构**。

**实测出来的洞**（修之前）：建甲、乙两院与一个挂甲院的 `doctor`，乙院建一次就诊，
甲院 doctor 提交病历——

    ② 甲院 doctor 写乙院就诊的病历 -> 201 {"created":true,"record":{"org_id":2,"doctor_name":"t3_doc_a",…}}
       病历 org_id=2 doctor=t3_doc_a

病历落在**乙院名下**，书写人却是甲院的医生；而且这条路径会同步跑 `_apply_qc` 算缺陷扣分，
**写进去的内容直接进乙院的质控成绩**。

这是本轮判定的三类洞里最没争议的一类：`org_id` 根本不由调用方给，是从 `encounter`
推出来的——"调用方以为自己在写本院"这种误用都不成立，能写到别家只可能是没校验。
（对照：ADR-0020 的调拨与 ADR-0021 附录里的签约都还有业务口径要裁定，这条没有。）
"""
from app.database import SessionLocal
from app.models import MedicalRecord
from conftest import login


def _org(client, admin, name, level="township"):
    return client.post("/api/organizations", headers=admin,
                       json={"name": name,
                             "org_type": "lead_hospital" if level == "county" else "township",
                             "level": level}).json()


def _encounter(client, admin, patient_id, org_id):
    return client.post("/api/encounters", headers=admin,
                       json={"patient_id": patient_id, "org_id": org_id,
                             "encounter_type": "outpatient"}).json()


def _record_count(encounter_id):
    with SessionLocal() as db:
        return (db.query(MedicalRecord)
                .filter(MedicalRecord.encounter_id == encounter_id).count())


def test_写别家就诊的病历必须403且不留任何行(client, admin):
    a = _org(client, admin, "病历甲院", "county")
    b = _org(client, admin, "病历乙院")
    client.post("/api/users", headers=admin,
                json={"username": "mr_doc_a", "password": "pass123456",
                      "role": "doctor", "org_id": a["id"]})
    doc_a = login(client, "mr_doc_a", "pass123456")
    p = client.post("/api/patients", headers=admin,
                    json={"name": "病历用例患者", "id_card": "330424199101011234",
                          "phone": "13700110070"}).json()
    enc = _encounter(client, admin, p["id"], b["id"])

    resp = client.post("/api/quality/records", headers=doc_a,
                       json={"encounter_id": enc["id"], "chief_complaint": "甲院写的主诉"})
    assert resp.status_code == 403, resp.text
    assert _record_count(enc["id"]) == 0, "403 之后不许留下病历行"


def test_写本院就诊的病历照常放行(client, admin):
    a = _org(client, admin, "本院病历甲院", "county")
    client.post("/api/users", headers=admin,
                json={"username": "mr_doc_own", "password": "pass123456",
                      "role": "doctor", "org_id": a["id"]})
    doc_a = login(client, "mr_doc_own", "pass123456")
    p = client.post("/api/patients", headers=admin,
                    json={"name": "本院病历患者", "id_card": "330424199102021234",
                          "phone": "13700110071"}).json()
    enc = _encounter(client, admin, p["id"], a["id"])

    resp = client.post("/api/quality/records", headers=doc_a,
                       json={"encounter_id": enc["id"], "chief_complaint": "本院主诉"})
    assert resp.status_code == 201, resp.text
    assert _record_count(enc["id"]) == 1


def test_全域角色跨机构写病历仍然放行(client, admin):
    """admin 在 GLOBAL_ROLES 里，平台侧代录不能被误伤。"""
    b = _org(client, admin, "全域病历乙院")
    p = client.post("/api/patients", headers=admin,
                    json={"name": "全域病历患者", "id_card": "330424199103031234",
                          "phone": "13700110072"}).json()
    enc = _encounter(client, admin, p["id"], b["id"])

    resp = client.post("/api/quality/records", headers=admin,
                       json={"encounter_id": enc["id"], "chief_complaint": "平台代录"})
    assert resp.status_code == 201, resp.text
