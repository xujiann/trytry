"""阶段三 门急诊文书：知情告知书、治疗处置记录、门急诊护理记录、手术质量。

知情同意这块的用例几乎全在盯**证据属性**：签署时的措辞会不会被后来的模板
修订改掉、拒签有没有被折叠成"没签"、已有结论的能不能被改写。
这些都不是功能问题，是这份文书还算不算数的问题。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post(
        "/api/organizations",
        json={"name": "门诊文书县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()


def _staff(client, admin, org, username, role):
    client.post(
        "/api/users",
        json={"username": username, "password": "passw0rd1", "role": role, "org_id": org["id"]},
        headers=admin,
    )
    resp = client.post("/api/auth/login", json={"username": username, "password": "passw0rd1"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def doctor(client, admin, org):
    return _staff(client, admin, org, "od_doc", "doctor")


@pytest.fixture(scope="module")
def operator(client, admin, org):
    return _staff(client, admin, org, "od_op", "operator")


@pytest.fixture(scope="module")
def patient(client, admin):
    return client.post(
        "/api/patients", json={"name": "门诊患者", "id_card": "331782199505051234"}, headers=admin
    ).json()


@pytest.fixture(scope="module")
def encounter(client, admin, org, patient):
    return client.post(
        "/api/encounters",
        json={"patient_id": patient["id"], "org_id": org["id"], "doctor_name": "门诊医师",
              "diagnosis_name": "急性上呼吸道感染"},
        headers=admin,
    ).json()


@pytest.fixture(scope="module")
def template(client, admin):
    return client.post(
        "/api/outpatient/consent-templates",
        json={"consent_type": "treatment", "title": "特殊治疗知情同意书",
              "body": "本人已了解该治疗的目的、风险与替代方案。", "version": "v1"},
        headers=admin,
    ).json()


# ---------------- 知情告知书 ----------------


def test_签署时冻结正文_模板改了不影响已签(client, admin, doctor, org, patient, template):
    """患者签的是签署当时那份措辞，模板后续修订不得回溯改变已签文书。"""
    consent = client.post(
        "/api/outpatient/consents",
        json={"patient_id": patient["id"], "org_id": org["id"], "consent_type": "treatment",
              "template_id": template["id"], "related_type": "encounter", "related_id": 0},
        headers=doctor,
    ).json()
    original = consent["content"]
    assert original == template["body"]
    assert consent["template_version"] == "v1"

    client.patch(
        f"/api/outpatient/consent-templates/{template['id']}",
        json={"body": "（修订版）本人已了解……并知悉新增的三项风险。", "version": "v2"},
        headers=admin,
    )
    after = client.get(
        f"/api/outpatient/consents?patient_id={patient['id']}", headers=doctor
    ).json()
    signed = next(c for c in after if c["id"] == consent["id"])
    assert signed["content"] == original, "模板修订回溯改写了已生成的告知书"
    assert signed["template_version"] == "v1"


def test_拒签是一等状态而不是没签(client, doctor, org, patient, template):
    """机构需要证明"告知过、对方拒绝了"；折叠进 pending 等于把证据抹掉。"""
    consent = client.post(
        "/api/outpatient/consents",
        json={"patient_id": patient["id"], "org_id": org["id"], "consent_type": "surgery",
              "title": "手术知情同意书", "content": "手术风险包括出血、感染……"},
        headers=doctor,
    ).json()
    resp = client.post(
        f"/api/outpatient/consents/{consent['id']}/refuse",
        json={"signer_name": "患者本人", "signer_relation": "self",
              "refuse_reason": "拟转上级医院手术"},
        headers=doctor,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "refused"
    assert body["status_name"] == "拒绝签署"
    assert body["refuse_reason"] == "拟转上级医院手术"
    assert body["signed_at"], "拒签也要记时间——什么时候拒的同样是证据"


def test_已有结论的告知书不可改写(client, doctor, org, patient):
    consent = client.post(
        "/api/outpatient/consents",
        json={"patient_id": patient["id"], "org_id": org["id"], "consent_type": "exam",
              "title": "增强CT知情同意书", "content": "造影剂过敏风险……"},
        headers=doctor,
    ).json()
    client.post(f"/api/outpatient/consents/{consent['id']}/sign",
                json={"signer_name": "李某", "signer_relation": "spouse"}, headers=doctor)
    for action, body in [
        ("sign", {"signer_name": "改签的人"}),
        ("refuse", {"signer_name": "改签的人", "refuse_reason": "反悔"}),
    ]:
        resp = client.post(f"/api/outpatient/consents/{consent['id']}/{action}",
                           json=body, headers=doctor)
        assert resp.status_code == 409


def test_签署人可以不是本人(client, doctor, org, patient):
    """未成年、意识障碍、委托家属都是常态，只记 patient 会丢掉真正签字的人。"""
    consent = client.post(
        "/api/outpatient/consents",
        json={"patient_id": patient["id"], "org_id": org["id"], "consent_type": "anesthesia",
              "title": "麻醉知情同意书", "content": "麻醉风险……"},
        headers=doctor,
    ).json()
    body = client.post(
        f"/api/outpatient/consents/{consent['id']}/sign",
        json={"signer_name": "张父", "signer_relation": "parent"}, headers=doctor,
    ).json()
    assert body["signer_name"] == "张父"
    assert body["signer_relation_name"] == "父母"


def test_停用模板不可再用于签署(client, admin, doctor, org, patient):
    tpl = client.post(
        "/api/outpatient/consent-templates",
        json={"consent_type": "other", "title": "旧版告知书", "body": "旧措辞"},
        headers=admin,
    ).json()
    client.patch(f"/api/outpatient/consent-templates/{tpl['id']}",
                 json={"active": False}, headers=admin)
    resp = client.post(
        "/api/outpatient/consents",
        json={"patient_id": patient["id"], "org_id": org["id"], "consent_type": "other",
              "template_id": tpl["id"]},
        headers=doctor,
    )
    assert resp.status_code == 409


def test_模板类型与告知类型必须一致(client, doctor, org, patient, template):
    resp = client.post(
        "/api/outpatient/consents",
        json={"patient_id": patient["id"], "org_id": org["id"], "consent_type": "transfusion",
              "template_id": template["id"]},
        headers=doctor,
    )
    assert resp.status_code == 422


def test_不用模板时必须自带标题与正文(client, doctor, org, patient):
    resp = client.post(
        "/api/outpatient/consents",
        json={"patient_id": patient["id"], "org_id": org["id"], "consent_type": "other"},
        headers=doctor,
    )
    assert resp.status_code == 422


def test_告知书限医师开具(client, operator, org, patient):
    """告知义务在医师，不能由窗口代劳。"""
    resp = client.post(
        "/api/outpatient/consents",
        json={"patient_id": patient["id"], "org_id": org["id"], "consent_type": "other",
              "title": "x", "content": "y"},
        headers=operator,
    )
    assert resp.status_code == 403


# ---------------- 治疗处置记录 ----------------


def test_处置记录挂在就诊上并可跨就诊查患者(client, doctor, encounter, patient):
    resp = client.post(
        f"/api/outpatient/encounters/{encounter['id']}/treatments",
        json={"treatment_name": "雾化吸入", "site": "口鼻", "dose": "布地奈德1mg",
              "executor_name": "王护士", "reaction": "无不适"},
        headers=doctor,
    )
    assert resp.status_code == 201
    assert resp.json()["patient_id"] == patient["id"]   # 从就诊带出，不用调用方传
    by_encounter = client.get(
        f"/api/outpatient/encounters/{encounter['id']}/treatments", headers=doctor
    ).json()
    by_patient = client.get(
        f"/api/outpatient/treatments?patient_id={patient['id']}", headers=doctor
    ).json()
    assert len(by_encounter) == 1 and len(by_patient) == 1


def test_反应留空表示未记录而不是无不适(client, doctor, encounter):
    """两者在纠纷里的分量完全不同，所以不设默认值。"""
    body = client.post(
        f"/api/outpatient/encounters/{encounter['id']}/treatments",
        json={"treatment_name": "清创换药"}, headers=doctor,
    ).json()
    assert body["reaction"] == ""


def test_处置记录的就诊不存在返回404(client, doctor):
    resp = client.post(
        "/api/outpatient/encounters/999999/treatments",
        json={"treatment_name": "雾化吸入"}, headers=doctor,
    )
    assert resp.status_code == 404


# ---------------- 门急诊护理记录 ----------------


def test_门急诊护理记录可独立于住院存在(client, doctor, encounter):
    """原先 nursing_records 只挂 admission_id，门急诊护理无处可落。"""
    resp = client.post(
        f"/api/outpatient/encounters/{encounter['id']}/nursing-records",
        json={"content": "输液中，滴速40滴/分，患者无主诉不适", "nurse_name": "李护士"},
        headers=doctor,
    )
    assert resp.status_code == 201
    rows = client.get(
        f"/api/outpatient/encounters/{encounter['id']}/nursing-records", headers=doctor
    ).json()
    assert len(rows) == 1 and rows[0]["nurse_name"] == "李护士"


def test_住院护理记录不受影响(client, admin, org, patient):
    """放宽挂载点是加法，不能把原有住院路径改坏。"""
    p2 = client.post(
        "/api/patients", json={"name": "住院护理患者", "id_card": "331782199505051299"},
        headers=admin,
    ).json()
    ward = client.post("/api/inpatient/wards", json={"org_id": org["id"], "name": "文书病区"},
                       headers=admin).json()
    bed = client.post("/api/inpatient/beds", json={"ward_id": ward["id"], "bed_no": "OD1"},
                      headers=admin).json()
    adm = client.post(
        "/api/inpatient/admissions",
        json={"patient_id": p2["id"], "ward_id": ward["id"], "bed_id": bed["id"],
              "doctor_name": "主治", "diagnosis_name": "肺炎"},
        headers=admin,
    ).json()
    resp = client.post(
        f"/api/inpatient/admissions/{adm['id']}/nursing-records",
        json={"content": "一级护理，生命体征平稳", "nurse_name": "赵护士",
              "recorded_at": "2026-08-12 10:00"},
        headers=admin,
    )
    assert resp.status_code == 201
    assert len(client.get(f"/api/inpatient/admissions/{adm['id']}/nursing-records",
                          headers=admin).json()) == 1


def test_门急诊完整性只列事实不下合格结论(client, doctor, encounter):
    """感冒开药就该只有病历，硬套住院那套"缺项即不合格"会逼人补假记录。"""
    data = client.get(
        f"/api/outpatient/encounters/{encounter['id']}/completeness", headers=doctor
    ).json()
    assert data["treatment_records"] == 2
    assert data["nursing_records"] == 1
    assert "不判合格与否" in data["note"]
    assert "grade" not in data and "qualified" not in data


# ---------------- 手术质量指标 ----------------


def test_手术质量指标进入临床指标接口(client, admin, org):
    """#14 要求的手术质量：并发症率与非计划重返率。"""
    data = client.get(f"/api/quality/clinical-indicators?org_id={org['id']}", headers=admin).json()
    keys = {i["key"] for i in data["indicators"]}
    assert {"surgery_complication", "unplanned_return"} <= keys
    for item in data["indicators"]:
        if item["dimension"] == "手术质量":
            assert item["caliber"]


def test_并发症口径明说只会低估(client, admin, org):
    """留空按"无"计是妥协——医师不会专门去填"无"。妥协要写在口径里。"""
    data = client.get(f"/api/quality/clinical-indicators?org_id={org['id']}", headers=admin).json()
    item = next(i for i in data["indicators"] if i["key"] == "surgery_complication")
    assert "低估" in item["caliber"]


def test_非计划重返不做推断(client, admin, org):
    item = next(
        i for i in client.get(
            f"/api/quality/clinical-indicators?org_id={org['id']}", headers=admin
        ).json()["indicators"]
        if i["key"] == "unplanned_return"
    )
    assert "不做推断" in item["caliber"]
