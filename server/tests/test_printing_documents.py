"""B2 打印单据补齐：8 类单据的 200 + 关键字段在文、口径边界与跨机构 403。

跨机构口径对齐 test_print_attachment_visibility：患者类单据一律先过
`assert_patient_visible`，乙院账号打甲院患者的任何单据都是 403。
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


def login(client, username, password):
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def world(client):
    """甲院完整业务现场（住院/体检/同意/接种/转诊），乙院只有一个旁观医师。"""
    admin = login(client, "admin", "admin123")
    org = client.post(
        "/api/organizations",
        json={"name": "打印补齐甲院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    org_b = client.post(
        "/api/organizations",
        json={"name": "打印补齐乙院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    township = client.post(
        "/api/organizations",
        json={"name": "打印补齐卫生院", "org_type": "township", "level": "township",
              "parent_id": org["id"]},
        headers=admin,
    ).json()
    patient = client.post(
        "/api/patients",
        json={"name": "打印乙", "id_card": "330281199007078019", "gender": "男",
              "birth_date": "1990-07-07", "phone": "13887654321"},
        headers=admin,
    ).json()
    accounts = {}
    for username, role, oid in (
        ("pd_doc", "doctor", org["id"]),
        ("pd_op", "operator", org["id"]),
        ("pd_ph", "public_health", org["id"]),
        ("pd_doc_b", "doctor", org_b["id"]),
    ):
        client.post(
            "/api/users",
            json={"username": username, "password": "pass123456", "role": role, "org_id": oid},
            headers=admin,
        )
        accounts[username] = login(client, username, "pass123456")
    doc, op, ph = accounts["pd_doc"], accounts["pd_op"], accounts["pd_ph"]

    # —— 住院链：病区/床位 → 入院 → 计费 → 结算 → 病案首页 → 出院 ——
    ward = client.post(
        "/api/inpatient/wards", json={"org_id": org["id"], "name": "内科一病区"}, headers=admin
    ).json()
    bed = client.post(
        "/api/inpatient/beds", json={"ward_id": ward["id"], "bed_no": "01"}, headers=admin
    ).json()
    admission = client.post(
        "/api/inpatient/admissions",
        json={"patient_id": patient["id"], "ward_id": ward["id"], "bed_id": bed["id"],
              "doctor_name": "钱医师", "diagnosis_name": "社区获得性肺炎"},
        headers=doc,
    ).json()
    client.post(
        "/api/billing/charge-items",
        json={"code": "BED-A", "name": "普通床位费", "category": "bed", "price": 60},
        headers=admin,
    )
    client.post(
        "/api/billing/charge-items",
        json={"code": "INJ-CEF", "name": "头孢曲松静脉输注", "category": "treatment", "price": 45.5},
        headers=admin,
    )
    for code, qty in (("BED-A", 3), ("INJ-CEF", 2)):
        r = client.post(
            "/api/billing/details",
            json={"patient_id": patient["id"], "admission_id": admission["id"],
                  "item_code": code, "quantity": qty},
            headers=op,
        )
        assert r.status_code == 201, r.text
    settlement = client.post(
        "/api/billing/settlements",
        json={"bill_type": "inpatient", "admission_id": admission["id"], "insurance_pay": 180},
        headers=op,
    ).json()
    client.post(
        f"/api/inpatient/admissions/{admission['id']}/case-summary",
        json={"discharge_diagnosis": "社区获得性肺炎（治愈）", "operation": "",
              "total_cost": 271.0, "drug_cost": 91.0, "outcome": "治愈"},
        headers=doc,
    )
    # 出院病程记录（出院小结的诊疗经过来源）
    client.post(
        f"/api/inpatient/admissions/{admission['id']}/progress-notes",
        json={"note_type": "discharge",
              "content": "入院后予抗感染治疗7天，体温正常3日，复查胸片吸收良好，予出院。"},
        headers=doc,
    )

    # —— 体检（分项 + 总检）——
    checkup = client.post(
        "/api/checkups",
        json={"patient_id": patient["id"], "org_id": org["id"], "exam_date": "2026-07-10",
              "summary": "血压偏高", "abnormal_items": "血压 150/95",
              "items": [{"item_code": "SBP", "item_name": "收缩压", "result_value": "150",
                         "unit": "mmHg", "ref_range": "90-140", "abnormal": True}]},
        headers=ph,
    ).json()
    client.post(
        f"/api/checkups/{checkup['id']}/review",
        json={"final_conclusion": "血压升高，建议心内科门诊复测确诊"},
        headers=doc,
    )

    # —— 知情同意（窗口代录，文本走幂等种子的 active 版）——
    consent = client.post(
        "/api/consents",
        json={"patient_id": patient["id"], "scene": "archive", "evidence": "签字影像附件#1"},
        headers=op,
    ).json()

    # —— 疫苗接种 ——
    vaccination = client.post(
        "/api/vaccination/records",
        json={"patient_id": patient["id"], "vaccine_code": "FLU", "vaccine_name": "流感疫苗",
              "dose_no": 1, "vaccinated_date": "2026-07-11", "org_id": org["id"],
              "site": "左上臂三角肌", "vaccinator": "孙护士"},
        headers=ph,
    ).json()

    # —— 转诊 ——
    referral = client.post(
        "/api/referrals",
        json={"patient_id": patient["id"], "from_org_id": township["id"],
              "to_org_id": org["id"], "direction": "up", "reason": "肺炎加重，请上级收治"},
        headers=doc,
    ).json()

    return {
        "admin": admin, "org": org, "doc": doc, "doc_b": accounts["pd_doc_b"],
        "patient": patient, "admission": admission, "settlement": settlement,
        "checkup": checkup, "consent": consent, "vaccination": vaccination,
        "referral": referral,
    }


def _html(client, headers, path):
    resp = client.get(path, headers=headers)
    assert resp.status_code == 200, f"{path}: {resp.text[:300]}"
    assert resp.headers["content-type"].startswith("text/html")
    return resp.text


def test_住院费用清单在文(client, world):
    html = _html(client, world["doc"], f"/api/print/inpatient-bills/{world['admission']['id']}")
    for kw in ["住院费用清单", "打印补齐甲院", "打印乙", "普通床位费", "头孢曲松静脉输注",
               "180.00", "91.00", "271.00", f"FY{world['admission']['id']:08d}"]:
        assert kw in html, kw


def test_出院前打印出院小结409(client, world):
    # 该用例在出院前执行序（fixture 中尚未出院），钉住"在院不出小结"口径
    resp = client.get(
        f"/api/print/discharge-summaries/{world['admission']['id']}", headers=world["doc"]
    )
    assert resp.status_code == 409


def test_住院结算单在文(client, world):
    html = _html(client, world["doc"], f"/api/print/settlements/{world['settlement']['id']}")
    for kw in ["住院结算单", "271.00", "180.00", "91.00", f"JS{world['settlement']['id']:08d}"]:
        assert kw in html, kw


def test_病案首页在文(client, world):
    html = _html(client, world["doc"], f"/api/print/case-summaries/{world['admission']['id']}")
    for kw in ["病案首页", "社区获得性肺炎（治愈）", "治愈", "271.00", "钱医师",
               f"BA{world['admission']['id']:08d}"]:
        assert kw in html, kw


def test_体检报告分项与总检在文(client, world):
    html = _html(client, world["doc"], f"/api/print/checkups/{world['checkup']['id']}")
    for kw in ["体检报告", "收缩压", "150", "90-140", "异常", "血压 150/95",
               "血压升高，建议心内科门诊复测确诊", "pd_doc", f"TJ{world['checkup']['id']:08d}"]:
        assert kw in html, kw


def test_知情同意书在文(client, world):
    html = _html(client, world["doc"], f"/api/print/consents/{world['consent']['id']}")
    for kw in ["知情同意书", "居民健康建档", "窗口代录", "签字影像附件#1",
               f"ZQ{world['consent']['id']:08d}"]:
        assert kw in html, kw


def test_疫苗接种证明在文(client, world):
    html = _html(client, world["doc"], f"/api/print/vaccinations/{world['vaccination']['id']}")
    for kw in ["疫苗接种证明", "流感疫苗", "第 1 剂", "左上臂三角肌", "孙护士",
               f"YM{world['vaccination']['id']:08d}"]:
        assert kw in html, kw


def test_转诊单在文(client, world):
    html = _html(client, world["doc"], f"/api/print/referrals/{world['referral']['id']}")
    for kw in ["转诊单", "打印补齐卫生院", "打印补齐甲院", "上转", "肺炎加重，请上级收治",
               f"ZZ{world['referral']['id']:08d}"]:
        assert kw in html, kw


def test_出院后出院小结在文(client, world):
    resp = client.post(
        f"/api/inpatient/admissions/{world['admission']['id']}/discharge", headers=world["doc"]
    )
    assert resp.status_code == 200, resp.text
    html = _html(client, world["doc"], f"/api/print/discharge-summaries/{world['admission']['id']}")
    for kw in ["出院小结", "社区获得性肺炎", "予出院", "治愈", "钱医师",
               f"CY{world['admission']['id']:08d}"]:
        assert kw in html, kw


def test_八类单据不存在都是404(client, world):
    for path in (
        "/api/print/inpatient-bills/99999",
        "/api/print/settlements/99999",
        "/api/print/case-summaries/99999",
        "/api/print/checkups/99999",
        "/api/print/consents/99999",
        "/api/print/vaccinations/99999",
        "/api/print/referrals/99999",
        "/api/print/discharge-summaries/99999",
    ):
        assert client.get(path, headers=world["doc"]).status_code == 404, path


def test_跨机构打印一律403(client, world):
    """乙院医师打甲院患者的 8 类单据全部 403（对齐 test_print_attachment_visibility 口径）。"""
    outsider = world["doc_b"]
    for path in (
        f"/api/print/inpatient-bills/{world['admission']['id']}",
        f"/api/print/settlements/{world['settlement']['id']}",
        f"/api/print/case-summaries/{world['admission']['id']}",
        f"/api/print/checkups/{world['checkup']['id']}",
        f"/api/print/consents/{world['consent']['id']}",
        f"/api/print/vaccinations/{world['vaccination']['id']}",
        f"/api/print/referrals/{world['referral']['id']}",
        f"/api/print/discharge-summaries/{world['admission']['id']}",
    ):
        assert client.get(path, headers=outsider).status_code == 403, path


def test_新单据类型可配模板(client, world):
    resp = client.put(
        "/api/print/templates",
        json={"doc_type": "vaccine_cert", "header_org_name": "县疾控中心（代章）",
              "footer_note": "本证明仅证明所列剂次接种事实", "show_qr": False},
        headers=world["admin"],
    )
    assert resp.status_code == 200, resp.text
    html = _html(client, world["admin"], f"/api/print/vaccinations/{world['vaccination']['id']}")
    assert "县疾控中心（代章）" in html
    assert "本证明仅证明所列剂次接种事实" in html
    assert "二维码" not in html
