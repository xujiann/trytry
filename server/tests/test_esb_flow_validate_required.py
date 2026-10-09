"""集成平台编排的校验步不写必填字段照样能存，跑起来「校验通过（0 项必填）」，这一步形同虚设（P2-1730，第五十一批扫描 AO1-4）。

修前（c67fa3b 实测，扫描脚本 r7_flow_cfg.py）：validate 步的 config 写成 `{"require": ["id_card", "name"]}`、
`{"fields": ["id_card"]}` 或 `{}`，建编排都是 201；缺证件号、缺姓名的报文执行后逐步结果为
`['校验通过（0 项必填）', '交换日志已落库']`，消息记成功。`_validate_steps` 只在写了 required 时查它的形状，`_steps_problem`
不看 validate 步——transform / persist / route 写错都在存的时候 422（P2-1122「取值要对得上运行期认得的」），只漏了 validate。

修后：`_steps_problem` 要求 validate 步的 required 是非空的字段名数组——建 / 改编排 422 并点名第几步；存量里这样写的编排
照 P2-1122 的同一条路径执行前 409，消息的状态、重试次数、错误说明原样不动，也不落执行记录。config 里的未知键不另拦。
"""
import pytest

from app.database import SessionLocal
from app.models import EsbFlow, EsbFlowRun, EsbMessage
from test_esb import enqueue, register_endpoint

LOG = {"type": "persist", "config": {"entity": "exchange_log", "source_system": "P21730", "message_type": "adt"}}
SAID = "第 2 步校验须用 required 写明必填字段"
#: 校验步写错的几种样子（放在第 2 步，报错要点到第 2 步）
BAD_CONFIGS = {
    "require": {"require": ["id_card", "name"]},   # 键名少个 d
    "fields": {"fields": ["id_card"]},             # 换了个键名
    "empty": {},                                   # 什么也没写
    "none": None,                                  # 连 config 都没有
    "blank_list": {"required": []},
    "blank_name": {"required": ["  "]},
}


def _steps(config):
    validate = {"type": "validate"} if config is None else {"type": "validate", "config": config}
    return [LOG, validate]


def _message(message_id):
    with SessionLocal() as db:
        row = db.get(EsbMessage, message_id)
        runs = db.query(EsbFlowRun).filter(EsbFlowRun.message_id == message_id).count()
        return row.status, row.retry_count, row.last_error, runs


@pytest.mark.parametrize("key", list(BAD_CONFIGS))
def test_校验步不写必填字段_建与改都422_点名第几步(client, admin, key):
    got = client.post("/api/esb/flows", headers=admin, json={
        "code": f"P21730_{key}", "name": key, "steps": _steps(BAD_CONFIGS[key])})
    assert got.status_code == 422 and SAID in got.json()["detail"], (key, got.status_code, got.text)   # 修前 201

    good = client.post("/api/esb/flows", headers=admin, json={
        "code": f"P21730_OK_{key}", "name": key, "steps": _steps({"required": ["id_card"]})})
    assert good.status_code == 201, good.text   # 写对的照收
    changed = client.patch(f"/api/esb/flows/{good.json()['id']}", headers=admin, json={"steps": _steps(BAD_CONFIGS[key])})
    assert changed.status_code == 422 and SAID in changed.json()["detail"], (key, changed.status_code, changed.text)   # 修前 200
    flows = {f["code"]: f for f in client.get("/api/esb/flows", headers=admin).json()}
    assert flows[f"P21730_OK_{key}"]["steps"] == _steps({"required": ["id_card"]}) and f"P21730_{key}" not in flows


def test_存量里不写必填字段的编排_执行前409_消息不动_改好再执行照常校验(client, admin):
    inbound = register_endpoint(client, admin, "P21730_HIS")
    with SessionLocal() as db:   # 修前存下的编排：绕过接口直接落库
        db.add_all([EsbFlow(code=f"P21730_OLD_{key}", name=key, steps=_steps(config)) for key, config in BAD_CONFIGS.items()])
        db.commit()
    message_id = enqueue(client, inbound, "adt", {"note": "缺证件号缺姓名"}, max_retries=1).json()["id"]

    for key in BAD_CONFIGS:
        got = client.post(f"/api/esb/flows/P21730_OLD_{key}/run?message_id={message_id}", headers=admin)
        assert got.status_code == 409 and SAID in got.json()["detail"], (key, got.status_code, got.text)   # 修前 200、成功
        assert _message(message_id) == ("queued", 0, "", 0), key   # 修前 succeeded，「校验通过（0 项必填）」

    old = next(f for f in client.get("/api/esb/flows", headers=admin).json() if f["code"] == "P21730_OLD_empty")
    fixed = client.patch(f"/api/esb/flows/{old['id']}", headers=admin, json={"steps": _steps({"required": ["id_card"]})})
    assert fixed.status_code == 200, fixed.text
    run = client.post(f"/api/esb/flows/P21730_OLD_empty/run?message_id={message_id}", headers=admin)
    assert run.status_code == 200, run.text
    assert run.json()["step_results"][-1] == {"step": 2, "type": "validate", "status": "failed",
                                              "detail": "必填字段缺失：id_card"}   # 真校验了
