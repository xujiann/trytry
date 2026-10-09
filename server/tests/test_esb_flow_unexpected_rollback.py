"""编排执行遇到意外错误回滚之后，前面「落库交换日志」那一步的执行记录仍写「succeeded / 交换日志已落库」（P2-1732，第五十一批扫描 AO1-10）。

修前（c67fa3b 实测，扫描脚本 r7_flow_cfg.py）：编排「落库交换日志 → 转换 → 落库患者」，第 3 步因 FHIR 的 name.text 是个
对象、绑定参数时报 ProgrammingError，`run_flow` 的 `except Exception` → `_unexpected` 先 `db.rollback()`，第 1 步只 `db.add`
进会话的交换日志随之撤掉（库里 0 行），执行记录里第 1 步却仍是「succeeded / 交换日志已落库」。`EsbFlowRun` 的逐步结果是
拿来回溯定位失败步骤的，前提是记录与实际一致。

修后：意外错误分支把写的行已随回滚撤掉的那几步改记 `rolled_back`、说明后缀「已随回滚撤销（第 N 步出现未预期错误）」；
已投出的路由、建档那一步中途提交的写入（`create_patient_idempotent` 新建时提交，此前加的交换日志随之入库）照实保留。
"""
from app.database import SessionLocal
from app.models import ExchangeLog, Patient
from app.routers import esb as esb_module
from test_esb import enqueue, register_endpoint


def _logs(source_system):
    with SessionLocal() as db:
        return db.query(ExchangeLog).filter(ExchangeLog.source_system == source_system).count()


def _patients(name):
    with SessionLocal() as db:
        return db.query(Patient).filter(Patient.name == name).count()


def _flow(client, admin, code, steps):
    resp = client.post("/api/esb/flows", headers=admin, json={"code": code, "name": code, "steps": steps})
    assert resp.status_code == 201, resp.text


def _log_step(mark):
    return {"type": "persist", "config": {"entity": "exchange_log", "source_system": mark, "message_type": "adt"}}


TRANSFORM = {"type": "transform", "config": {"format": "fhir_patient"}}
PERSIST_PATIENT = {"type": "persist", "config": {"entity": "patient"}}


def _resource(id_card, name):
    return {"resourceType": "Patient", "gender": "male", "birthDate": "1990-03-07", "name": [{"text": name}],
            "identifier": [{"system": "urn:oid:2.16.156.10011.1.3", "value": id_card}]}


def test_第3步库报错回滚_第1步落库交换日志记已随回滚撤销_库里确实没有(client, admin):
    inbound = register_endpoint(client, admin, "P21732_HIS")
    _flow(client, admin, "P21732_R7", [_log_step("P21732_R7MARK"), TRANSFORM, PERSIST_PATIENT])
    bad = _resource("110101199003071732", {"zh": "王五"})   # name.text 是个对象：落库绑定参数时 ProgrammingError
    message_id = enqueue(client, inbound, "adt", {"resource": bad}).json()["id"]

    body = client.post(f"/api/esb/flows/P21732_R7/run?message_id={message_id}", headers=admin)
    assert body.status_code == 200, body.text
    steps = body.json()["step_results"]
    assert steps[0] == {"step": 1, "type": "persist", "status": "rolled_back",
                        "detail": "交换日志已落库——已随回滚撤销（第 3 步出现未预期错误）"}, steps   # 修前 succeeded / 交换日志已落库
    assert (steps[1]["type"], steps[1]["status"]) == ("transform", "succeeded")   # 不写库的步骤照实
    assert (steps[2]["type"], steps[2]["status"]) == ("persist", "failed") and "ProgrammingError" in steps[2]["detail"]
    assert _logs("P21732_R7MARK") == 0   # 库里确实没有那行日志
    runs = client.get(f"/api/esb/flow-runs?message_id={message_id}", headers=admin).json()
    assert runs[0]["step_results"] == steps   # 落库的执行记录与回执一致
    assert (body.json()["message_status"], body.json()["retry_count"]) == ("failed", 1)   # 意外错误照旧计一次失败


def test_已投出的路由与建档中途提交的写入照实保留_只改记提交之后才加的(client, admin, monkeypatch):
    inbound = register_endpoint(client, admin, "P21732_HIS2")
    for code in ("P21732_T1", "P21732_T2"):
        register_endpoint(client, admin, code, system_type="provincial", direction="outbound",
                          endpoint_url=f"https://prov.example/{code}")
    delivered = []

    def deliver(endpoint, msg_type, body):
        if endpoint.code == "P21732_T2":
            raise RuntimeError("对端 SDK 里的意外")
        delivered.append(endpoint.code)
        return f"已投递（delivered）至 {endpoint.endpoint_url}，HTTP 200"

    monkeypatch.setattr(esb_module, "_deliver", deliver)
    _flow(client, admin, "P21732_MIX", [
        {"type": "route", "config": {"target_endpoint": "P21732_T1"}},   # 1 已投出
        _log_step("P21732_KEPT"),                                         # 2 随第 4 步建档的提交入库
        TRANSFORM,                                                        # 3
        PERSIST_PATIENT,                                                  # 4 新建档案：中途提交
        _log_step("P21732_GONE"),                                         # 5 只在会话里，随回滚撤掉
        {"type": "route", "config": {"target_endpoint": "P21732_T2"}},   # 6 意外错误
    ])
    message_id = enqueue(client, inbound, "adt", {"resource": _resource("330281199003071732", "P21732 1732")}).json()["id"]

    body = client.post(f"/api/esb/flows/P21732_MIX/run?message_id={message_id}", headers=admin)
    assert body.status_code == 200, body.text
    steps = body.json()["step_results"]
    assert [(s["step"], s["status"]) for s in steps] == [
        (1, "succeeded"), (2, "succeeded"), (3, "succeeded"), (4, "succeeded"), (5, "rolled_back"), (6, "failed")], steps
    assert steps[0]["detail"].startswith("路由至 ") and delivered == ["P21732_T1"]
    assert steps[1]["detail"] == "交换日志已落库" and _logs("P21732_KEPT") == 1
    assert "患者档案新建" in steps[3]["detail"] and _patients("P21732 1732") == 1
    assert steps[4]["detail"] == "交换日志已落库——已随回滚撤销（第 6 步出现未预期错误）" and _logs("P21732_GONE") == 0
    assert "RuntimeError" in steps[5]["detail"]
