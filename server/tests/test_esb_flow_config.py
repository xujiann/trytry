"""集成平台编排的步骤取值写错照存，每执行一次记成消息的一次失败、到上限转死信（P2-1122，第三十二批扫描 B4-3）。

修前（b3069f0 实测）：`_validate_steps` 只查步骤类型与配置项的形状——转换格式写成 `fhir`、落库实体写成 `patients`、
路由到不存在的接入方 `LIS9`、先落库后转换，建编排都是 201；各对一条 `max_retries=1` 的 FHIR 建档消息执行，全部
`run=failed msg=dead retry=1`，修好编排后再执行 409「消息当前状态 死信 不可再消费」——HIS 推一次就不再推的建档报文
就此丢失。编排写错是配置问题，却记在每条被执行的消息上。

修后：建编排与改编排同一套取值校验（`esb._steps_problem`，取值就是运行期 `_run_step` 认的那几种）422；存量里写错的
编排执行前 409，消息的状态、重试次数、错误说明原样不动，也不落执行记录；改好编排再执行照常建档。
"""
from app.database import SessionLocal
from app.models import EsbFlow, EsbFlowRun, EsbMessage
from test_esb import FHIR_PATIENT, enqueue, register_endpoint

GOOD_STEPS = [{"type": "transform", "config": {"format": "fhir_patient"}},
              {"type": "persist", "config": {"entity": "patient"}}]
#: 编码 → (写错的步骤, 报错里要点出的那一句)
BAD_FLOWS = {
    "P21122_FMT": ([{"type": "transform", "config": {"format": "fhir"}},
                    {"type": "persist", "config": {"entity": "patient"}}], "第 1 步未知转换格式 fhir"),
    "P21122_ENT": ([{"type": "transform", "config": {"format": "fhir_patient"}},
                    {"type": "persist", "config": {"entity": "patients"}}], "第 2 步未知落库实体 patients"),
    "P21122_ROUTE": ([{"type": "route", "config": {"target_endpoint": "LIS9"}}], "第 1 步路由目标接入方 LIS9 不存在"),
    "P21122_ORDER": ([{"type": "persist", "config": {"entity": "patient"}},
                      {"type": "transform", "config": {"format": "fhir_patient"}}],
                     "第 1 步落库患者档案须先经 transform"),
}


def _flows(client, admin):
    return {f["code"]: f for f in client.get("/api/esb/flows", headers=admin).json()}


def _message(message_id):
    with SessionLocal() as db:
        row = db.get(EsbMessage, message_id)
        runs = db.query(EsbFlowRun).filter(EsbFlowRun.message_id == message_id).count()
        return row.status, row.retry_count, row.last_error, runs


def test_四种写错的编排_建与改都422_写对的照收(client, admin):
    for code, (steps, said) in BAD_FLOWS.items():
        got = client.post("/api/esb/flows", headers=admin, json={"code": code, "name": code, "steps": steps})
        assert got.status_code == 422 and said in got.json()["detail"], (code, got.status_code, got.text)   # 修前 201

    good = client.post("/api/esb/flows", headers=admin, json={"code": "P21122_GOOD", "name": "建档", "steps": GOOD_STEPS})
    assert good.status_code == 201, good.text
    for code, (steps, said) in BAD_FLOWS.items():
        got = client.patch(f"/api/esb/flows/{good.json()['id']}", headers=admin, json={"steps": steps})
        assert got.status_code == 422 and said in got.json()["detail"], (code, got.status_code, got.text)   # 修前 200
    flows = _flows(client, admin)
    assert flows["P21122_GOOD"]["steps"] == GOOD_STEPS and not set(BAD_FLOWS) & set(flows)

    # 不误伤：落库交换日志不要求先转换；persist 缺省实体（patient）前面有转换；路由目标停用了照收（启用与否留运行期判）
    target = register_endpoint(client, admin, "P21122_OFF", system_type="provincial", direction="outbound")
    client.patch(f"/api/esb/endpoints/{target['id']}", headers=admin, json={"active": False})
    for code, steps in {
        "P21122_LOG": [{"type": "persist", "config": {"entity": "exchange_log"}}],
        "P21122_DEFAULT": [{"type": "transform", "config": {"format": "hl7v2_patient"}}, {"type": "persist"}],
        "P21122_OFFROUTE": [{"type": "route", "config": {"target_endpoint": "P21122_OFF"}}],
    }.items():
        got = client.post("/api/esb/flows", headers=admin, json={"code": code, "name": code, "steps": steps})
        assert got.status_code == 201, (code, got.text)


def test_存量写错的编排执行前409_消息计数与状态不动_改好再执行照常建档(client, admin):
    inbound = register_endpoint(client, admin, "P21122_HIS")
    with SessionLocal() as db:   # 修前存下的坏编排：绕过接口直接落库
        db.add_all([EsbFlow(code=f"OLD_{code}", name=code, steps=steps) for code, (steps, _said) in BAD_FLOWS.items()])
        db.commit()
    message_id = enqueue(client, inbound, "fhir_patient", {"resource": FHIR_PATIENT}, max_retries=1).json()["id"]

    for code, (_steps, said) in BAD_FLOWS.items():
        got = client.post(f"/api/esb/flows/OLD_{code}/run?message_id={message_id}", headers=admin)
        assert got.status_code == 409 and said in got.json()["detail"], (code, got.status_code, got.text)   # 修前 200、死信
        assert _message(message_id) == ("queued", 0, "", 0), code   # 修前 dead / 1 / 那一步的错误 / 一条失败执行记录

    old = _flows(client, admin)["OLD_P21122_FMT"]
    fixed = client.patch(f"/api/esb/flows/{old['id']}", headers=admin, json={"steps": GOOD_STEPS})
    assert fixed.status_code == 200, fixed.text
    run = client.post(f"/api/esb/flows/OLD_P21122_FMT/run?message_id={message_id}", headers=admin)
    assert run.status_code == 200 and run.json()["message_status"] == "succeeded", run.text   # 修前 409 死信不可再消费
    assert "EHC" in run.json()["step_results"][1]["detail"]
