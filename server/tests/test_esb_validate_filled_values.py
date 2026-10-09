"""集成平台编排的校验步把 0 / 0.0 / false 当成「必填字段缺失」（P2-1731，第五十一批扫描 AO1-5）。

修前（c67fa3b 实测，扫描脚本 r6_walk.py 的 (c) 段）：`_run_step` 判缺失用 `not str(data.get(f, "") or "").strip()`，`or ""`
把所有假值折成空串——检验结果 `{"abnormal_flag": 0, "result": 0.0}` 记 `failed 必填字段缺失：abnormal_flag、result`，
`{"abnormal_flag": false}` 同样判缺失；消息记失败、扣重试次数，按编排重试永远过不去，最后进死信。

修后：「没填」只指键不存在、None，或文字里一个看得见的字符都没有（与数据质控「为空」同一判据，P2-1148）；0、0.0、false
是填了的值。
"""
import pytest

from test_esb import enqueue, register_endpoint

FIELD = "abnormal_flag"


@pytest.fixture(scope="module")
def inbound(client, admin):
    endpoint = register_endpoint(client, admin, "P21731_LIS", system_type="lis", rate_limit_per_min=1000)
    flow = client.post("/api/esb/flows", headers=admin, json={"code": "P21731_F", "name": "检验结果校验", "steps": [
        {"type": "validate", "config": {"required": [FIELD, "result"]}},
        {"type": "persist", "config": {"entity": "exchange_log", "source_system": "P21731", "message_type": "lab"}}]})
    assert flow.status_code == 201, flow.text
    return endpoint


def _run(client, admin, endpoint, payload):
    message_id = enqueue(client, endpoint, "lab_result", payload).json()["id"]
    got = client.post(f"/api/esb/flows/P21731_F/run?message_id={message_id}", headers=admin)
    assert got.status_code == 200, got.text
    return got.json()


@pytest.mark.parametrize("value", [0, 0.0, False, "0", "阴性"], ids=["int0", "float0", "false", "str0", "text"])
def test_零与false是填了的值_校验通过_消息记成功(client, admin, inbound, value):
    body = _run(client, admin, inbound, {FIELD: value, "result": 0.0})
    assert body["status"] == "succeeded", body   # 修前 0 / 0.0 / false 记「必填字段缺失」
    assert body["step_results"][0]["detail"] == "校验通过（2 项必填）"
    assert (body["message_status"], body["retry_count"]) == ("succeeded", 0)


@pytest.mark.parametrize("payload", [{FIELD: None}, {FIELD: ""}, {FIELD: "  "}, {FIELD: "​"}, {}],
                         ids=["none", "empty", "spaces", "zero_width", "absent"])
def test_没填的照旧判缺失(client, admin, inbound, payload):
    body = _run(client, admin, inbound, {**payload, "result": 5.6})
    assert body["status"] == "failed" and body["error"] == f"必填字段缺失：{FIELD}", body
    assert (body["message_status"], body["retry_count"]) == ("failed", 1)
