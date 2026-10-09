"""ORU 的 OBX-8 写成带组件的形式时按第 1 组件判表 0078（P2-1756，第五十二批扫描 AP2-1）。

v2.7 起 OBX-8 是 CWE（码^文字^码表）：`HH^Critical high^HL70078`；本地 LIS 也有 `H^偏高` 这样带中文说明的写法。
`_oru_report` 原先只按 `~` 拆重复、整串进集合比对——修前实测：血钾 `HH^Critical high^HL70078`、血钠 `H^High^HL70078`
回 201「异常 0 项」、critical False，结论写「另 2 项的异常标志平台不认得（HH^CRITICAL HIGH^HL70078、…）」；危急值不进
闭环（不在危急值清单、不置「已通知」），走的是非危急分支，居民照收「报告已出具」。同一函数里 OBX-3 / OBX-6 早就先按
`^` 拆组件（P2-724），表 0078 的判据见 P1-213。

修法：每个重复先取第 1 组件再按表 0078 判；结论与所见里照旧印原文。第 1 组件空着的照旧整个重复当认不得的标志。
"""
import pytest

from app.database import SessionLocal
from app.models import ExamReport, SmsCode
from app.routers.portal import _reset_portal_failures
from app.sms import set_sms_provider

PATIENT_ID_CARD = "330102198807071756"
PHONE = "13700001756"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21756 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21756 检验患者", "id_card": PATIENT_ID_CARD, "gender": "男", "phone": PHONE})
    assert patient.status_code in (200, 201), patient.text
    # 居民账户：手机号唯一命中档案，登录即自动绑定——看「报告已出具」发没发给居民
    _reset_portal_failures()   # 进程内的发码限流可能被前面的模块用满
    set_sms_provider(None)
    with SessionLocal() as db:
        db.query(SmsCode).filter(SmsCode.phone == PHONE).delete()
        db.commit()
    code = client.post("/api/portal/auth/sms/code", json={"phone": PHONE, "purpose": "login"}).json()["debug_code"]
    login = client.post("/api/portal/auth/sms/login", json={"phone": PHONE, "code": code}).json()
    assert login["bound"] is True, login
    return {"org": org, "patient": patient.json()["id"], "me": {"Authorization": f"Bearer {login['access_token']}"}}


def _send(client, admin, world, control_id, *obx):
    req = client.post("/api/exams", headers=admin, json={
        "patient_id": world["patient"], "from_org_id": world["org"], "center_type": "lab",
        "item_code": "LYTE", "item_name": "电解质"})
    assert req.status_code == 201, req.text
    message = "\r".join([
        f"MSH|^~\\&|LIS|XZYY|MEDPLAT|COUNTY|20261009100000||ORU^R01|{control_id}|P|2.7",
        f"PID|1||{PATIENT_ID_CARD}^^^CN^ID||P21756 检验患者",
        f"OBR|1|{req.json()['id']}||LYTE^电解质",
        *obx,
    ])
    resp = client.post("/api/integration/hl7v2/oru", headers=admin, json={"message": message})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _report(report_id):
    with SessionLocal() as db:   # 非危急报告不进危急值清单，直接读库
        report = db.get(ExamReport, report_id)
        return report.conclusion, report.critical_status


def _resident_report_notes(client, world, report_id):
    notes = client.get("/api/portal/me/notifications", headers=world["me"]).json()
    return [n["title"] for n in notes if n["category"] == "exam_report" and n["link_id"] == report_id]


def test_CWE写法的HH判危急_进危急值清单_不给居民发已出具(client, admin, world):
    body = _send(client, admin, world, "P21756A",
                 "OBX|1|NM|K^血钾||6.9|mmol/L|3.5-5.3|HH^Critical high^HL70078",
                 "OBX|2|NM|NA^血钠||150|mmol/L|137-147|H^High^HL70078")
    assert (body["abnormal_count"], body["critical"]) == (2, True)   # 修前 (0, False)
    critical = {r["id"]: r for r in client.get("/api/exams/critical", headers=admin).json()}
    assert body["report_id"] in critical                             # 修前不在危急值清单里
    assert critical[body["report_id"]]["critical_status"] == "notified"
    # 危急项照旧印原文；修前还多一句「另 2 项的异常标志平台不认得」
    assert critical[body["report_id"]]["conclusion"] == (
        "电解质：共 2 项，异常 2 项，含危急值（血钾 6.9 mmol/L [HH^CRITICAL HIGH^HL70078]）")
    assert _resident_report_notes(client, world, body["report_id"]) == []   # 修前居民照收「报告已出具」


def test_本地带中文说明的H计异常_非危急照常通知居民(client, admin, world):
    body = _send(client, admin, world, "P21756B",
                 "OBX|1|NM|NA^血钠||150|mmol/L|137-147|H^偏高",
                 "OBX|2|NM|K^血钾||4.1|mmol/L|3.5-5.3|N^正常")
    assert (body["abnormal_count"], body["critical"]) == (1, False)   # 修前 (0, False)
    assert _report(body["report_id"]) == ("电解质：共 2 项，异常 1 项", "")   # 修前写「平台不认得（H^偏高、N^正常）」
    assert _resident_report_notes(client, world, body["report_id"]) == ["电解质 报告已出具"]


def test_带组件的认不得标志照旧写进结论_第1组件空着的也不当正常(client, admin, world):
    body = _send(client, admin, world, "P21756C",
                 "OBX|1|NM|CRP^C反应蛋白||12|mg/L|0-10|W^Worse^HL70078",
                 "OBX|2|NM|TG^甘油三酯||1.2|mmol/L|0.4-1.7|^偏高")
    assert (body["abnormal_count"], body["critical"]) == (0, False)
    assert _report(body["report_id"])[0] == (
        "电解质：共 2 项，异常 0 项，另 2 项的异常标志平台不认得（W^WORSE^HL70078、^偏高），以原文为准")


def test_重复里带组件的逐个判(client, admin, world):
    body = _send(client, admin, world, "P21756D", "OBX|1|NM|K^血钾||2.1|mmol/L|3.5-5.3|W^Worse^HL70078~LL^Critical low")
    assert (body["abnormal_count"], body["critical"]) == (1, True)   # 修前 (0, False)
