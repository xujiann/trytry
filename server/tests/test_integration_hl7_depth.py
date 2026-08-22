"""工程包 I1：HL7 v2 入站深度——ORU^R01 检验结果回写、ADT 事件细分、白名单 422 与交换日志留证。"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app

ID_CARD = "330281199203046014"
OTHER_ID_CARD = "330281198802027015"


def adt(event, control_id="MSG01", pid=True, pv1="", dg1=""):
    lines = [f"MSH|^~\\&|HIS|XZYY|MEDPLAT|COUNTY|20260821090000||{event}|{control_id}|P|2.4"]
    if pid:
        lines.append(f"PID|1||{ID_CARD}^^^CN^ID||李入院||19920304|M|||杭州市||13800001111")
    if pv1:
        lines.append(pv1)
    if dg1:
        lines.append(dg1)
    return "\r".join(lines)


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
def admin(client):
    return login(client, "admin", "admin123")


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post(
        "/api/organizations",
        json={"name": "HL7深度县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()


@pytest.fixture(scope="module")
def ward_bed(client, admin, org):
    ward = client.post(
        "/api/inpatient/wards", json={"org_id": org["id"], "name": "内科病区"}, headers=admin
    ).json()
    bed = client.post(
        "/api/inpatient/beds", json={"ward_id": ward["id"], "bed_no": "12"}, headers=admin
    ).json()
    return ward, bed


def send_adt(client, headers, message):
    return client.post("/api/integration/hl7v2/adt", json={"message": message}, headers=headers)


# ---------------------------------------------------------------------------
# ADT 事件细分
# ---------------------------------------------------------------------------


def test_adt_a04_creates_patient_idempotent(client, admin):
    resp = send_adt(client, admin, adt("ADT^A04"))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["event"] == "ADT^A04" and body["created"] is True
    assert body["patient"]["name"] == "李入院"
    assert "MSA|AA|MSG01" in body["ack"]
    again = send_adt(client, admin, adt("ADT^A04")).json()
    assert again["created"] is False


def test_adt_a08_updates_patient_fields(client, admin):
    msg = (
        "MSH|^~\\&|HIS|XZYY|MEDPLAT|COUNTY|20260821091000||ADT^A08|MSG08|P|2.4\r"
        f"PID|1||{ID_CARD}^^^CN^ID||李入院改||19920304|M|||杭州市||13911112222"
    )
    body = send_adt(client, admin, msg).json()
    assert body["event"] == "ADT^A08" and body["detail"] == "患者信息已更新"
    patient = client.get("/api/patients?keyword=李入院改", headers=admin).json()[0]
    assert patient["phone"] == "13911112222"
    # 档案不存在的 A08 按规范§四拒收
    unknown = (
        "MSH|^~\\&|HIS|X|M|C|20260821||ADT^A08|MSG08X|P|2.4\r"
        "PID|1||110101190001011234^^^CN^ID||查无此人||19000101|M"
    )
    assert send_adt(client, admin, unknown).status_code == 404


def test_adt_a01_admits_via_pv1_location(client, admin, org, ward_bed):
    ward, bed = ward_bed
    msg = adt(
        "ADT^A01",
        control_id="MSGA01",
        pv1="PV1|1|I|内科病区^301^12||||1001^王^主任",
        dg1="DG1|1||I10^高血压",
    )
    resp = send_adt(client, admin, msg)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["admission_id"] and body["encounter_id"]
    admissions = client.get(
        f"/api/inpatient/admissions?patient_id={body['patient']['id']}", headers=admin
    ).json()
    assert admissions[0]["status"] == "admitted"
    assert admissions[0]["ward_id"] == ward["id"] and admissions[0]["bed_id"] == bed["id"]
    assert admissions[0]["doctor_name"] == "王主任"
    assert admissions[0]["diagnosis_name"] == "高血压"
    # 床位被原子占用
    beds = client.get(f"/api/inpatient/beds?ward_id={ward['id']}", headers=admin).json()
    assert beds[0]["status"] == "occupied"
    # 重复入院 → 复用住院登记的 409 口径
    assert send_adt(client, admin, msg).status_code == 409


def test_adt_a03_discharges_and_releases_bed(client, admin, ward_bed):
    ward, _ = ward_bed
    body = send_adt(client, admin, adt("ADT^A03", control_id="MSGA03")).json()
    assert body["event"] == "ADT^A03" and body["admission_id"]
    admissions = client.get("/api/inpatient/admissions?status=discharged", headers=admin).json()
    assert any(a["id"] == body["admission_id"] for a in admissions)
    beds = client.get(f"/api/inpatient/beds?ward_id={ward['id']}", headers=admin).json()
    assert beds[0]["status"] == "free"
    # 无在院记录的再次 A03 → 409
    assert send_adt(client, admin, adt("ADT^A03")).status_code == 409


def test_adt_unsupported_event_422_with_exchange_log(client, admin):
    resp = send_adt(client, admin, adt("ADT^A02"))
    assert resp.status_code == 422
    assert "A01/A03/A04/A08" in resp.json()["detail"]
    # 坏报文也要在交换日志留证（消息类型细分到事件）
    logs = client.get(
        "/api/integration/exchange-logs?message_type=hl7v2_adt_a02", headers=admin
    ).json()
    assert logs["logs"] and logs["logs"][0]["success"] is False
    # ORU 消息发到 ADT 入口同样 422（白名单不放行）
    oru = "MSH|^~\\&|LIS|X|M|C|20260821||ORU^R01|Z1|P|2.4\rPID|1||330281199203046014^^^CN^ID||张三"
    assert send_adt(client, admin, oru).status_code == 422


def test_adt_missing_pv1_rejected(client, admin):
    resp = send_adt(client, admin, adt("ADT^A01"))  # 无 PV1 段
    assert resp.status_code == 422
    assert "PV1" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# ORU^R01 检验结果
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def exam_request(client, admin, org):
    patient = client.get("/api/patients?keyword=李入院", headers=admin).json()[0]
    resp = client.post(
        "/api/exams",
        json={
            "patient_id": patient["id"],
            "from_org_id": org["id"],
            "center_type": "lab",
            "item_code": "GLU",
            "item_name": "血糖组合",
        },
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def oru(request_id, obx_lines, control_id="LAB01", pid_id_card=ID_CARD):
    lines = [
        f"MSH|^~\\&|LIS|XZYY|MEDPLAT|COUNTY|20260821100000||ORU^R01|{control_id}|P|2.4",
        f"PID|1||{pid_id_card}^^^CN^ID||李入院改",
        f"OBR|1|{request_id}||GLU^血糖组合",
        *obx_lines,
    ]
    return "\r".join(lines)


def test_oru_multi_obx_writes_report(client, admin, exam_request):
    msg = oru(
        exam_request["id"],
        [
            "OBX|1|NM|GLU^空腹血糖|1|9.8|mmol/L^毫摩尔每升|3.9-6.1|HH",
            "OBX|2|NM|HBA1C^糖化血红蛋白|1|8.1|%|4-6|H",
            "OBX|3|NM|TG^甘油三酯|1|1.2|mmol/L|0.4-1.7|",
        ],
    )
    resp = client.post("/api/integration/hl7v2/oru", json={"message": msg}, headers=admin)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["request_id"] == exam_request["id"] and body["report_id"]
    assert body["obx_count"] == 3 and body["abnormal_count"] == 2
    # 非空洞断言②：OBX-8 的 HH 必须判为危急值（去掉异常标志解析即红）
    assert body["critical"] is True
    assert "MSA|AA|LAB01" in body["ack"]

    # 危急值报告进入既有闭环，finding 含 值/单位/参考范围/异常标志
    critical = client.get("/api/exams/critical", headers=admin).json()
    report = next(r for r in critical if r["id"] == body["report_id"])
    assert "空腹血糖：9.8 mmol/L（参考 3.9-6.1） [HH]" in report["finding"]
    assert "甘油三酯：1.2 mmol/L（参考 0.4-1.7）" in report["finding"]
    assert "危急值" in report["conclusion"] and report["critical_status"] == "notified"
    # 申请单状态推进为已报告
    reported = client.get("/api/exams?status=reported", headers=admin).json()
    assert any(r["id"] == exam_request["id"] for r in reported)
    # 成功入站落交换日志
    logs = client.get(
        "/api/integration/exchange-logs?message_type=hl7v2_oru_r01", headers=admin
    ).json()
    assert logs["logs"] and logs["logs"][0]["success"] is True

    # 已出报告的申请单再次回传 → 409（一单一报告）
    again = client.post("/api/integration/hl7v2/oru", json={"message": msg}, headers=admin)
    assert again.status_code == 409


def test_oru_unknown_request_rejected_404_with_log(client, admin):
    msg = oru(999999, ["OBX|1|NM|GLU^空腹血糖|1|5.0|mmol/L|3.9-6.1|"])
    resp = client.post("/api/integration/hl7v2/oru", json={"message": msg}, headers=admin)
    assert resp.status_code == 404
    assert "先创建检查申请" in resp.json()["detail"]
    logs = client.get(
        "/api/integration/exchange-logs?message_type=hl7v2_oru_r01&success=false", headers=admin
    ).json()
    assert any("999999" in log["error_detail"] for log in logs["logs"])


def test_oru_patient_mismatch_rejected(client, admin, org, exam_request):
    # 另一个患者的身份证出现在 PID：防串单
    client.post(
        "/api/patients",
        json={"name": "旁人", "id_card": OTHER_ID_CARD},
        headers=admin,
    )
    patient = client.get("/api/patients?keyword=李入院", headers=admin).json()[0]
    req = client.post(
        "/api/exams",
        json={
            "patient_id": patient["id"],
            "from_org_id": org["id"],
            "center_type": "lab",
            "item_code": "CBC",
            "item_name": "血常规",
        },
        headers=admin,
    ).json()
    msg = oru(req["id"], ["OBX|1|NM|WBC^白细胞|1|6.0|10^9/L|4-10|"], pid_id_card=OTHER_ID_CARD)
    resp = client.post("/api/integration/hl7v2/oru", json={"message": msg}, headers=admin)
    assert resp.status_code == 422
    assert "不一致" in resp.json()["detail"]


def test_oru_requires_obr_obx_and_r01(client, admin, exam_request):
    no_obr = "MSH|^~\\&|LIS|X|M|C|20260821||ORU^R01|Z2|P|2.4\rOBX|1|NM|GLU^血糖|1|5.0|mmol/L||"
    assert (
        client.post("/api/integration/hl7v2/oru", json={"message": no_obr}, headers=admin).status_code
        == 422
    )
    no_obx = f"MSH|^~\\&|LIS|X|M|C|20260821||ORU^R01|Z3|P|2.4\rOBR|1|{exam_request['id']}||GLU^血糖"
    assert (
        client.post("/api/integration/hl7v2/oru", json={"message": no_obx}, headers=admin).status_code
        == 422
    )
    adt_msg = adt("ADT^A04")
    resp = client.post("/api/integration/hl7v2/oru", json={"message": adt_msg}, headers=admin)
    assert resp.status_code == 422 and "ORU^R01" in resp.json()["detail"]
