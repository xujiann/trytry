"""块1：报告打印——检查报告/处方/申请单/证明的 A4 可打印 HTML、脱敏、模板生效与404。"""
import pytest

from conftest import login


@pytest.fixture(scope="module")
def fixtures(client, admin):
    org = client.post(
        "/api/organizations",
        json={"name": "打印测试县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    patient = client.post(
        "/api/patients",
        json={
            "name": "打印甲",
            "id_card": "330281199203046017",
            "gender": "男",
            "birth_date": "1992-03-04",
            "phone": "13812345678",
        },
        headers=admin,
    ).json()
    accounts = {}
    for username, role in [("pr_doc", "doctor"), ("pr_ph", "public_health")]:
        client.post(
            "/api/users",
            json={"username": username, "password": "pass123456", "role": role, "org_id": org["id"]},
            headers=admin,
        )
        accounts[role] = login(client, username, "pass123456")
    doctor = accounts["doctor"]
    # 检查申请 → 领取 → 出危急值报告
    req = client.post(
        "/api/exams",
        json={
            "patient_id": patient["id"],
            "from_org_id": org["id"],
            "center_type": "lab",
            "item_code": "K-POTASSIUM",
            "item_name": "血清钾测定",
            "clinical_info": "乏力待查",
        },
        headers=doctor,
    ).json()
    client.post(f"/api/exams/{req['id']}/claim", headers=doctor)
    report = client.post(
        f"/api/exams/{req['id']}/report",
        json={
            "finding": "血清钾 6.9 mmol/L",
            "conclusion": "重度高钾血症",
            "critical": True,
        },
        headers=doctor,
    ).json()
    rx = client.post(
        "/api/prescriptions",
        json={
            "patient_id": patient["id"],
            "org_id": org["id"],
            "diagnosis_name": "高钾血症",
            "items": [
                {"drug_code": "D-CAGLU", "drug_name": "葡萄糖酸钙注射液", "daily_dose": 1.0, "days": 1}
            ],
        },
        headers=doctor,
    ).json()
    cert = client.post(
        "/api/certs",
        json={
            "cert_type": "death",
            "name": "打印甲",
            "gender": "男",
            "event_date": "2026-05-01",
            "detail": "高钾血症致心搏骤停",
            "org_id": org["id"],
            "patient_id": patient["id"],
        },
        headers=accounts["public_health"],
    ).json()
    return {
        "org": org,
        "patient": patient,
        "doctor": doctor,
        "request": req,
        "report": report,
        "rx": rx,
        "cert": cert,
    }


def test_exam_report_print_contains_key_fields(client, admin, fixtures):
    resp = client.get(f"/api/print/exam-reports/{fixtures['report']['id']}", headers=admin)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/html")
    html = resp.text
    for keyword in [
        "打印测试县医院",  # 机构抬头
        "检查检验报告单",
        "打印甲",  # 患者姓名
        "血清钾测定",  # 项目
        "血清钾 6.9 mmol/L",  # 所见
        "重度高钾血症",  # 结论
        "危急值",  # 危急值标记
        "报告医师",
        "报告时间",
        "扫码验真",  # 验真二维码（ADR-0015：占位框已替换为真码）
        "<svg",  # 真 SVG 码而非占位框
        "打印时间",  # 页脚
        "@page",  # A4 版式
        "@media print",
    ]:
        assert keyword in html, keyword


def test_print_desensitization_matches_api_rule(client, admin, fixtures):
    """脱敏规则与业务接口一致：admin 明文、非 admin 掩码（前4后4/前3后2）。"""
    admin_html = client.get(
        f"/api/print/exam-reports/{fixtures['report']['id']}", headers=admin
    ).text
    assert "330281199203046017" in admin_html
    assert "13812345678" in admin_html
    doctor_html = client.get(
        f"/api/print/exam-reports/{fixtures['report']['id']}", headers=fixtures["doctor"]
    ).text
    assert "330281199203046017" not in doctor_html
    assert "3302**********6017" in doctor_html
    assert "13812345678" not in doctor_html
    assert "138******78" in doctor_html


def test_prescription_print(client, admin, fixtures):
    html = client.get(f"/api/print/prescriptions/{fixtures['rx']['id']}", headers=admin).text
    assert "处方笺" in html
    assert "葡萄糖酸钙注射液" in html
    assert "高钾血症" in html
    assert "开方医师" in html


def test_exam_request_print(client, admin, fixtures):
    html = client.get(f"/api/print/exam-requests/{fixtures['request']['id']}", headers=admin).text
    assert "检查检验申请单" in html
    assert "K-POTASSIUM" in html
    assert "乏力待查" in html
    assert "申请医师" in html


def test_cert_print(client, admin, fixtures):
    html = client.get(f"/api/print/certs/{fixtures['cert']['id']}", headers=admin).text
    assert "死亡医学证明" in html
    assert fixtures["cert"]["cert_no"] in html
    assert "高钾血症致心搏骤停" in html


def test_missing_documents_return_404(client, admin):
    for path in [
        "/api/print/exam-reports/99999",
        "/api/print/prescriptions/99999",
        "/api/print/exam-requests/99999",
        "/api/print/certs/99999",
    ]:
        assert client.get(path, headers=admin).status_code == 404, path


def test_print_requires_login(client, fixtures):
    assert client.get(f"/api/print/exam-reports/{fixtures['report']['id']}").status_code == 401


def test_template_defaults_listed_and_upsert_applies(client, admin, fixtures):
    listed = client.get("/api/print/templates", headers=admin).json()
    # B2 补齐 8 类单据后，默认占位清单与 DOC_TYPES 同步（原 4 类仍必须在）
    from app.routers.printing import DOC_TYPES

    assert {t["doc_type"] for t in listed} == set(DOC_TYPES)
    assert {"exam_report", "prescription", "exam_request", "cert"} <= set(DOC_TYPES)
    assert all(t["id"] is None for t in listed)  # 尚未配置时给出默认占位
    resp = client.put(
        "/api/print/templates",
        json={
            "doc_type": "exam_report",
            "header_org_name": "县域医共体总院（模板抬头）",
            "footer_note": "本报告仅对送检样本负责",
            "show_qr": False,
        },
        headers=admin,
    )
    assert resp.status_code == 200, resp.text
    html = client.get(f"/api/print/exam-reports/{fixtures['report']['id']}", headers=admin).text
    assert "县域医共体总院（模板抬头）" in html
    assert "本报告仅对送检样本负责" in html
    assert "扫码验真" not in html and "<svg" not in html  # show_qr=False 时不渲染验真码
    # 未配置模板的单据类型不受影响，仍走机构名与默认页脚
    rx_html = client.get(f"/api/print/prescriptions/{fixtures['rx']['id']}", headers=admin).text
    assert "打印测试县医院" in rx_html
    assert "扫码验真" in rx_html and "<svg" in rx_html


def test_template_upsert_is_admin_only(client, fixtures):
    resp = client.put(
        "/api/print/templates",
        json={"doc_type": "cert", "header_org_name": "越权抬头"},
        headers=fixtures["doctor"],
    )
    assert resp.status_code == 403


def test_template_upsert_idempotent(client, admin):
    first = client.put(
        "/api/print/templates",
        json={"doc_type": "prescription", "header_org_name": "处方抬头A"},
        headers=admin,
    ).json()
    second = client.put(
        "/api/print/templates",
        json={"doc_type": "prescription", "header_org_name": "处方抬头B"},
        headers=admin,
    ).json()
    assert first["id"] == second["id"]  # doc_type 唯一，二次提交更新不新增
    assert second["header_org_name"] == "处方抬头B"
