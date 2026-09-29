"""一条 ORU^R01 带几张申请的结果时按 OBR 分组，各回各的申请单（P2-722，第十九批「导入 / 入站对接 vs 界面录入」扫描 K3-3）。

LIS 常把同一次采血的几张申请（血常规、电解质）放进一条 ORU：每个 OBR 后面跟着它自己的 OBX。原先只认第一个 OBR、却把
全文的 OBX 都算进去——电解质的血钾 6.9（HH）写进了血常规的报告、危急值挂在血常规上；电解质那张申请永远「待出报告」，
ACK 照回 AA，LIS 不会重发，开电解质的医生永远拿不到结果。

修法：按 OBR 分组，每组各自定位申请单、各自核 PID、各自出报告；先把各组都核对完再出报告，任何一组不对整条拒收
（422 / 404 / 409），不留下「前一组已出具、后一组被拒」的半截。单组消息的回执一字不变，多组另在 `reports` 里逐组列出。
"""
import pytest

ID_CARD = "330281199206066015"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2722 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={"name": "P2722 患者", "id_card": ID_CARD}).json()["id"]
    return {"org": org, "patient": patient}


def _request(client, admin, world, code, name):
    resp = client.post("/api/exams", headers=admin, json={
        "patient_id": world["patient"], "from_org_id": world["org"], "center_type": "lab",
        "item_code": code, "item_name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _oru(*groups, control_id="P2722"):
    lines = [f"MSH|^~\\&|LIS|XZYY|MEDPLAT|COUNTY|20260929100000||ORU^R01|{control_id}|P|2.4",
             f"PID|1||{ID_CARD}^^^CN^ID||P2722 患者"]
    for request_id, item, obx in groups:
        lines.append(f"OBR|1|{request_id}||{item}")
        lines.extend(obx)
    return "\r".join(lines)


CBC = ["OBX|1|NM|WBC^白细胞|1|6.2|10^9/L|3.5-9.5|N", "OBX|2|NM|HGB^血红蛋白|1|128|g/L|115-150|N"]
LYTES = ["OBX|1|NM|K^血钾|1|6.9|mmol/L|3.5-5.3|HH", "OBX|2|NM|NA^血钠|1|139|mmol/L|137-147|N"]


def _report_of(client, admin, request_id):
    rows = client.get("/api/exams?status=reported", headers=admin).json()
    assert any(r["id"] == request_id for r in rows), f"申请单 {request_id} 没有出报告"
    critical = {r["request_id"]: r for r in client.get("/api/exams/critical", headers=admin).json()}
    return critical.get(request_id)


def test_两张申请一条消息_各回各的申请单_危急值只挂在电解质上(client, admin, world):
    cbc = _request(client, admin, world, "CBC", "血常规")
    lytes = _request(client, admin, world, "LYTES", "电解质")
    resp = client.post("/api/integration/hl7v2/oru", headers=admin, json={
        "message": _oru((cbc, "CBC^血常规", CBC), (lytes, "LYTES^电解质", LYTES))})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert [(r["request_id"], r["obx_count"], r["critical"]) for r in body["reports"]] == [
        (cbc, 2, False), (lytes, 2, True)]   # 修前：只出了血常规一份、4 项、含危急值，电解质仍待出报告
    assert (body["request_id"], body["obx_count"], body["critical"]) == (cbc, 4, True)
    assert _report_of(client, admin, cbc) is None   # 血常规不是危急值报告
    lytes_report = _report_of(client, admin, lytes)
    assert lytes_report is not None and "血钾：6.9 mmol/L" in lytes_report["finding"]
    assert "白细胞" not in lytes_report["finding"]


def test_后一组的申请单不存在_整条拒收_前一组也不出报告(client, admin, world):
    cbc = _request(client, admin, world, "CBC", "血常规")
    resp = client.post("/api/integration/hl7v2/oru", headers=admin, json={
        "message": _oru((cbc, "CBC^血常规", CBC), (999999, "LYTES^电解质", LYTES), control_id="P2722B")})
    assert resp.status_code == 404, resp.text
    pending = client.get("/api/exams?status=pending", headers=admin).json()
    assert any(r["id"] == cbc for r in pending)   # 没留下半截：前一组没有出具


@pytest.mark.parametrize("message_kind", ["同一申请两组", "某组没有结果"])
def test_同一申请出现两次或某组没有结果_422(client, admin, world, message_kind):
    cbc = _request(client, admin, world, "CBC", "血常规")
    other = _request(client, admin, world, "LYTES", "电解质")
    groups = ([(cbc, "CBC^血常规", CBC), (cbc, "CBC^血常规", CBC)] if message_kind == "同一申请两组"
              else [(cbc, "CBC^血常规", CBC), (other, "LYTES^电解质", [])])
    resp = client.post("/api/integration/hl7v2/oru", headers=admin, json={"message": _oru(*groups)})
    assert resp.status_code == 422, resp.text


def test_单组消息回执照旧(client, admin, world):
    cbc = _request(client, admin, world, "CBC", "血常规")
    resp = client.post("/api/integration/hl7v2/oru", headers=admin, json={
        "message": _oru((cbc, "CBC^血常规", CBC), control_id="P2722C")})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert (body["request_id"], body["obx_count"], body["abnormal_count"], body["critical"]) == (cbc, 2, 0, False)
    assert body["reports"] == [{"request_id": cbc, "report_id": body["report_id"], "obx_count": 2,
                                "abnormal_count": 0, "critical": False}]
