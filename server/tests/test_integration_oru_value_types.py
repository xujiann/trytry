"""ORU 的所见按 OBX-2 值类型取文字，附件类结果不灌进所见（P2-1758，第五十二批扫描 AP2-7）。

`_oru_report` 原先不看 OBX-2，OBX-5 只还原转义就印（OBX-3 / OBX-6 早就先按 `^` 拆组件，P2-724）。修前实测：
- CE / SN / 重复值把组件符原样印进所见：「乙肝表面抗原：P^阳性^99LAB」「表面抗原定量：>^250 IU/mL」「说明：第一行~第二行」；
- ED（PDF 报告单的 base64）排在前面时整段灌进所见，所见 2048 字全是 `^application^pdf^Base64^JVBERi0x…`，后面的危急血钾行
  被截断挤掉了（结论里仍点名了危急项）。

修法：CE / CWE（及同构的 CNE）取文字组件（第 2 组件，空则第 1）；SN 拼成「>250」；重复值用「；」连；ED / RP 不进所见，
这一项只印一句「附件类结果（ED），平台未收」（PDF 正文怎么收随 P2-1137）。异常 / 危急照旧按 OBX-8 判，与值类型无关。
"""
import base64

import pytest

from app.database import SessionLocal
from app.models import ExamReport

PATIENT_ID_CARD = "330102197308081758"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21758 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21758 检验患者", "id_card": PATIENT_ID_CARD, "gender": "女"})
    assert patient.status_code in (200, 201), patient.text
    return {"org": org, "patient": patient.json()["id"]}


def _send(client, admin, world, control_id, *obx, item=("HBV", "乙肝两对半")):
    req = client.post("/api/exams", headers=admin, json={
        "patient_id": world["patient"], "from_org_id": world["org"], "center_type": "lab",
        "item_code": item[0], "item_name": item[1]})
    assert req.status_code == 201, req.text
    message = "\r".join([
        f"MSH|^~\\&|LIS|XZYY|MEDPLAT|COUNTY|20261009100000||ORU^R01|{control_id}|P|2.4",
        f"PID|1||{PATIENT_ID_CARD}^^^CN^ID||P21758 检验患者",
        f"OBR|1|{req.json()['id']}||{item[0]}^{item[1]}",
        *obx,
    ])
    resp = client.post("/api/integration/hl7v2/oru", headers=admin, json={"message": message})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    with SessionLocal() as db:
        report = db.get(ExamReport, body["report_id"])
        return body, report.finding, report.conclusion


def test_编码类取文字_SN拼成比较式_重复值用分号连(client, admin, world):
    body, finding, _ = _send(client, admin, world, "P21758A",
                             "OBX|1|CE|HBsAg^乙肝表面抗原||P^阳性^99LAB||阴性|A",
                             "OBX|2|SN|HBsAgQ^表面抗原定量||>^250|IU/mL|<0.05|H",
                             "OBX|3|SN|HBeAg^e抗原||<^0.01|S/CO|<1.0|N",
                             "OBX|4|CWE|HBeAb^e抗体||^阴性~NEG^^99LAB||阴性|N",
                             "OBX|5|SN|TITER^抗体滴度||^1^:^128",
                             "OBX|6|TX|NOTE^说明||第一行~第二行")
    assert finding.split("\n") == [
        "乙肝表面抗原：阳性（参考 阴性） [A]",            # 修前「P^阳性^99LAB」
        "表面抗原定量：>250 IU/mL（参考 <0.05） [H]",     # 修前「>^250」
        "e抗原：<0.01 S/CO（参考 <1.0） [N]",             # 修前「<^0.01」
        "e抗体：阴性；NEG（参考 阴性） [N]",              # 文字组件空着取第 1 组件
        "抗体滴度：1:128",
        "说明：第一行；第二行",                            # 修前「第一行~第二行」
    ]
    assert (body["obx_count"], body["abnormal_count"], body["critical"]) == (6, 2, False)   # 判定照旧按 OBX-8


def test_ED在前_血钾行仍在所见里_仍判危急(client, admin, world):
    pdf = base64.b64encode(b"%PDF-1.4\n" + b"0" * 3000).decode()
    body, finding, conclusion = _send(client, admin, world, "P21758B",
                                      f"OBX|1|ED|PDF^报告单||^application^pdf^Base64^{pdf}",
                                      "OBX|2|NM|K^血钾||6.9|mmol/L|3.5-5.3|HH",
                                      "OBX|3|RP|IMG^影像||http://pacs.example/img/1^PACS^image^jpeg",
                                      item=("LYTE", "电解质"))
    assert finding.split("\n") == [
        "报告单：附件类结果（ED），平台未收",               # 修前 2048 字全是 base64
        "血钾：6.9 mmol/L（参考 3.5-5.3） [HH]",           # 修前被截断挤掉
        "影像：附件类结果（RP），平台未收",
    ]
    assert "JVBERi0x" not in finding
    assert (body["obx_count"], body["abnormal_count"], body["critical"]) == (3, 1, True)
    assert conclusion == "电解质：共 3 项，异常 1 项，含危急值（血钾 6.9 mmol/L [HH]）"


def test_数值与文本单值照旧(client, admin, world):
    _, finding, _ = _send(client, admin, world, "P21758C",
                          "OBX|1|NM|GLU^血糖||5.1|mmol/L|3.9-6.1|N",
                          r"OBX|2|ST|BC^血培养||金黄色葡萄球菌\S\生长|||AA",
                          item=("MIX", "检验组合"))
    assert finding.split("\n") == ["血糖：5.1 mmol/L（参考 3.9-6.1） [N]", "血培养：金黄色葡萄球菌^生长 [AA]"]
