"""简化建档口 /hl7v2/patient 只收建档类消息：A08 / A03 / ORU 等不再回 AA 吞掉（P2-1266，第三十七批「出站报文与上报的
标准符合度」扫描 AA3-3）。

修前全程不看 MSH-9：A08（改电话）、A03（出院）、ORU（检验结果）发到这里一律 201 + `MSA|AA`，实际只按证件号查档或建档——
A08 回执里的电话还是旧号，A03、ORU 照样拿 PID 新建一份档案；对端收到 AA 不再重发，交换日志记成功，信息更新、出院、结果
全丢。兄弟入口 /hl7v2/adt、/hl7v2/oru 各有白名单、其余 422 明确拒收。ESB 的 hl7v2_patient 转换 / 消费复用同一解析，
A08 照样记「成功」。

修后：A03 / A08 指路 /hl7v2/adt、ORU^R01 指路 /hl7v2/oru，其余 ADT 事件与非 ADT 消息「平台不受理该事件」，一律 422、
落交换日志、不建档；A04 / A28 与不带 MSH-9 的简化消息照旧 201，回执字节不变；ESB 一侧同一判据，照解析失败的老路记失败。
（A01 本口照旧收、收不收待裁定，由既有用例钉着，这里不加钉。）
"""
import re

import pytest

from app.database import SessionLocal
from app.models import Patient
from app.routers.patients import id_card_match

PATIENT = "/api/integration/hl7v2/patient"
SOURCE = "HIS-P21266"
_WEIGHTS = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]


def _id_card(body17: str) -> str:
    return body17 + "10X98765432"[sum(int(c) * w for c, w in zip(body17, _WEIGHTS)) % 11]


def _msg(msh9: str, control_id: str, id_card: str, phone: str = "13800000001") -> str:
    return "\r".join([f"MSH|^~\\&|HIS|XZYY|MEDPLAT|COUNTY|20261003090000||{msh9}|{control_id}|P|2.4",
                      f"PID|1||{id_card}^^^CN^ID||P21266 患者||19750101|M|||||{phone}"])


def _send(client, admin, message):
    return client.post(PATIENT, headers={**admin, "X-Source-System": SOURCE}, json={"message": message})


def _phones(id_card: str) -> list[str]:
    with SessionLocal() as db:
        return [p.phone for p in db.query(Patient).filter(id_card_match(id_card)).order_by(Patient.id)]


def _failures(client, admin, message_type: str, *needles: str) -> int:
    """交换日志里这类消息、错误详情含全部 needles 的失败条数（前后各数一次，差 1 即这一条落了失败）。"""
    logs = client.get("/api/integration/exchange-logs", headers=admin,
                      params={"message_type": message_type, "success": "false", "limit": 500}).json()["logs"]
    return sum(all(n in log["error_detail"] for n in needles) for log in logs)


def test_A08打简化建档口_422指路adt_档案电话不改_交换日志记失败(client, admin):
    id_card = _id_card("33010219750101123")
    created = _send(client, admin, _msg("ADT^A04", "P21266-1", id_card, phone="13800000001"))
    assert created.status_code == 201 and created.json()["created"] is True, created.text

    before = _failures(client, admin, "hl7v2_patient", "422: ", "ADT^A08")
    resp = _send(client, admin, _msg("ADT^A08", "P21266-2", id_card, phone="13900000002"))
    assert resp.status_code == 422, resp.text   # 修前 201 + MSA|AA，回执里的电话还是旧号
    assert "/api/integration/hl7v2/adt" in resp.json()["detail"], resp.json()
    assert _phones(id_card) == ["13800000001"]
    assert _failures(client, admin, "hl7v2_patient", "422: ", "ADT^A08") == before + 1   # 修前记成功


@pytest.mark.parametrize("msh9, body17, hint", [
    ("ADT^A03", "33010219750101131", "/api/integration/hl7v2/adt"),    # 修前 201 created=True：出院没办，倒新建了档案
    ("ADT^A08", "33010219750101158", "/api/integration/hl7v2/adt"),
    ("ORU^R01", "33010219750101166", "/api/integration/hl7v2/oru"),    # 修前 201：检验结果没进
    ("ADT^A02", "33010219750101174", "平台不受理该事件"),                # /adt 白名单之外的 ADT 事件
    ("ORM^O01", "33010219750101182", "平台不受理该事件"),                # 非 ADT 消息
], ids=["A03", "A08未建档", "ORU", "A02", "非ADT"])
def test_非建档事件_422指路_不建档_交换日志记失败(client, admin, msh9, body17, hint):
    id_card = _id_card(body17)
    before = _failures(client, admin, "hl7v2_patient", "422: ", msh9)
    resp = _send(client, admin, _msg(msh9, f"P21266-{msh9.replace('^', '-')}", id_card))
    assert resp.status_code == 422, resp.text
    assert set(resp.json()) == {"detail"} and hint in resp.json()["detail"], resp.json()
    assert msh9 in resp.json()["detail"]
    assert _phones(id_card) == []   # 修前按 PID 建了档
    assert _failures(client, admin, "hl7v2_patient", "422: ", msh9) == before + 1


ACK = r"MSH\|\^~\\&\|MEDPLAT\|COUNTY\|\|\|\d{{14}}\+0000\|\|ACK\|{cid}\|P\|2\.4\rMSA\|AA\|{cid}"


@pytest.mark.parametrize("msh9, body17", [
    ("ADT^A04", "33010219750101190"),
    ("ADT^A28", "33010219750101203"),             # 新增人员信息，同属建档
    ("", "33010219750101211"),                    # 不带 MSH-9 的简化消息，存量对接照旧收
    ("ADT^A04^ADT_A01", "33010219750101238"),     # MSH-9 带第三组件（消息结构）照样认
], ids=["A04", "A28", "无MSH-9", "A04带消息结构"])
def test_建档类消息照旧201_回执字节不变(client, admin, msh9, body17):
    id_card = _id_card(body17)
    cid = f"P21266-OK-{body17[-3:]}"
    resp = _send(client, admin, _msg(msh9, cid, id_card))
    assert resp.status_code == 201, resp.text
    assert resp.json()["created"] is True
    assert re.fullmatch(ACK.format(cid=re.escape(cid)), resp.json()["ack"]), resp.json()["ack"]
    assert _phones(id_card) == ["13800000001"]


# ---------------------------------------------------------------- ESB 一侧：同一解析、同一判据


def _esb_endpoint(client, admin, code):
    resp = client.post("/api/esb/endpoints", headers=admin,
                       json={"code": code, "name": f"接入方{code}", "system_type": "his", "direction": "inbound"})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _esb_consume(client, admin, endpoint, message):
    queued = client.post("/api/esb/messages", json={"msg_type": "hl7v2_patient", "payload": {"message": message},
                                                    "max_retries": 2},
                         headers={"X-Esb-Endpoint": endpoint["code"], "X-Esb-Token": endpoint["auth_token"]})
    assert queued.status_code == 201, queued.text
    resp = client.post(f"/api/esb/messages/{queued.json()['id']}/process", headers=admin)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_ESB的hl7v2_patient同一判据_A08记失败不建档_A28照常建档(client, admin):
    endpoint = _esb_endpoint(client, admin, "HIS_P21266")
    a08_card = _id_card("33010219750101246")
    before = _failures(client, admin, "esb_hl7v2_patient", "ADT^A08")
    a08 = _esb_consume(client, admin, endpoint, _msg("ADT^A08", "P21266-ESB-1", a08_card))
    assert a08["status"] == "failed" and a08["retry_count"] == 1, a08   # 修前 succeeded：按 PID 建档、更新丢了
    assert "/api/integration/hl7v2/adt" in a08["last_error"], a08["last_error"]
    assert _phones(a08_card) == []
    assert _failures(client, admin, "esb_hl7v2_patient", "ADT^A08") == before + 1

    a28_card = _id_card("33010219750101254")
    a28 = _esb_consume(client, admin, endpoint, _msg("ADT^A28", "P21266-ESB-2", a28_card))
    assert a28["status"] == "succeeded", a28
    assert _phones(a28_card) == ["13800000001"]
