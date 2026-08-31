"""集成平台底座 `/api/esb` 十三个端点的**特征化网 + 响应契约**。

套路同 `test_quality_contract.py`：先补网钉住**当前**响应的完整 JSON（dict 相等）
与键序 → 再加 `response_model` → 加完逐字节不变（CLAUDE.md §11）。

本簇的建模判断（都以此处的精确断言为依据）：

- `payload` 是接入方投进来的任意 JSON 对象（JSON 列原样透传）——宽 `dict` 建模，
  嵌套结构与非 ASCII 键都要原样回显，不猜内部结构（workflows.nodes 先例）。
- `flows.steps` 同理：`_validate_steps` 只检查 `type`/`config`，步骤 dict 里的
  **额外自定义键必须原样保留**——逐字段建模会把它们静默滤掉，此处用带
  `note` 键的步骤钉死。
- `step_results` 反之是**固定形状**：`run_flow` 是唯一产地，每个元素恒为
  {step, type, status, detail} 四键（历史上从未有过别的形状），照
  `quality.defects` 先例逐字段建模，成功/失败两条分支都在此钉住。
- 注册/轮换令牌两个端点比列表行**多一个尾键 `auth_token`**（明文仅此一次），
  与 PATCH/GET 的 11 键回执不同形——两个模型（继承加尾键，键序不变）。
- 统计里的 int/float 之别：`total`/`succeeded`/`dead`/`queued`/`failed`/`backlog`/
  `endpoints`/`flows` 恒为 int（COUNT 与 int 累加），`*_rate_pct` 恒为 float
  （`round(x*100.0/n, 2)` 与兜底字面量 `0.0` 两条分支都是浮点）——
  声明成 float 会把 `5` 变 `5.0`，即改字节。
- `next_retry_at` 是 `str | None`（键恒在，值随状态）：queued/succeeded/dead 为
  null，failed 为 ISO 字符串——不是条件键，无需 exclude_unset。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app

ENDPOINT_KEY_ORDER = [
    "id", "code", "name", "system_type", "system_type_name",
    "direction", "direction_name", "active", "rate_limit_per_min",
    "endpoint_url", "created_at",
]
MESSAGE_KEY_ORDER = [
    "id", "endpoint_id", "endpoint_code", "msg_type", "payload",
    "status", "status_name", "retry_count", "max_retries",
    "last_error", "next_retry_at", "created_at", "updated_at",
]
FLOW_KEY_ORDER = ["id", "code", "name", "steps", "step_count", "active", "created_at"]
STEP_RESULT_KEY_ORDER = ["step", "type", "status", "detail"]

#: 嵌套 + 非 ASCII 键 + 小数：payload 必须原样透传（宽 dict 建模的全部依据）
PAYLOAD_1 = {"a": 1, "nested": {"x": [1, 2], "flag": True}, "备注": "中文", "金额": 12.5}
#: 步骤里带自定义键 note：契约不得把它滤掉（宽 dict 建模的全部依据）
FLOW_STEPS = [{"type": "validate", "config": {"required": ["b"]}, "note": "自定义键要原样透传"}]


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
def seed(client, admin):
    """一次种完全部场景，测试只做断言——统计端点要按这份完整清单精确对账。

    消息终态清单（全部在 ep1 上，另有 1 条 queued 在 ep2 上）：
    msg1 succeeded（透传消费）· msg2 dead（空载荷 × max_retries=1）·
    msg3 succeeded（编排成功）· msg4 failed（手工消费失败 1 次 + 编排失败 1 次）·
    msg5 queued（从未消费）。
    """
    data: dict = {}
    resp = client.post(
        "/api/esb/endpoints",
        json={"code": "CT_HIS", "name": "契约医院HIS", "system_type": "his"},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    data["ep1"] = resp.json()
    data["ep2"] = client.post(
        "/api/esb/endpoints",
        json={
            "code": "CT_PROV",
            "name": "契约省平台",
            "system_type": "provincial",
            "direction": "outbound",
            "rate_limit_per_min": 5,
            "endpoint_url": "https://prov.example/ingest",
            "secret": "prov-secret",
        },
        headers=admin,
    ).json()
    data["ep1_patched"] = client.patch(
        f"/api/esb/endpoints/{data['ep1']['id']}",
        json={"name": "契约医院HIS改", "rate_limit_per_min": 99},
        headers=admin,
    ).json()
    data["ep2_rotated"] = client.post(
        f"/api/esb/endpoints/{data['ep2']['id']}/rotate-token", headers=admin
    ).json()

    def enqueue(endpoint, token, msg_type, payload, **extra):
        resp = client.post(
            "/api/esb/messages",
            json={"msg_type": msg_type, "payload": payload, **extra},
            headers={"X-Esb-Endpoint": endpoint["code"], "X-Esb-Token": token},
        )
        assert resp.status_code == 201, resp.text
        return resp.json()

    tok1 = data["ep1"]["auth_token"]
    data["msg1"] = enqueue(data["ep1"], tok1, "generic", PAYLOAD_1)
    data["msg1_done"] = client.post(
        f"/api/esb/messages/{data['msg1']['id']}/process", headers=admin
    ).json()
    data["msg2"] = enqueue(data["ep1"], tok1, "generic", {}, max_retries=1)
    data["msg2_dead"] = client.post(
        f"/api/esb/messages/{data['msg2']['id']}/process", headers=admin
    ).json()
    data["msg3"] = enqueue(data["ep1"], tok1, "generic", {"b": 2})
    data["msg4"] = enqueue(data["ep1"], tok1, "generic", {})
    data["msg4_failed"] = client.post(
        f"/api/esb/messages/{data['msg4']['id']}/process", headers=admin
    ).json()
    data["msg5"] = enqueue(data["ep1"], tok1, "generic", {"c": 3})
    # 轮换后的新令牌可用（旧令牌已失效由 test_esb.py 守）；出站端点仅入队不消费
    data["msg6"] = enqueue(data["ep2"], data["ep2_rotated"]["auth_token"], "report", {"k": "v"})

    data["flow1"] = client.post(
        "/api/esb/flows",
        json={"code": "CT_FLOW", "name": "契约编排", "steps": FLOW_STEPS},
        headers=admin,
    ).json()
    data["flow2"] = client.post(
        "/api/esb/flows",
        json={
            "code": "CT_FLOW_BAD",
            "name": "必失败编排",
            "steps": [{"type": "validate", "config": {"required": ["must_have"]}}],
        },
        headers=admin,
    ).json()
    data["flow1_patched"] = client.patch(
        f"/api/esb/flows/{data['flow1']['id']}", json={"name": "契约编排改名"}, headers=admin
    ).json()
    data["run1"] = client.post(
        f"/api/esb/flows/CT_FLOW/run?message_id={data['msg3']['id']}", headers=admin
    ).json()
    data["run2"] = client.post(
        f"/api/esb/flows/CT_FLOW_BAD/run?message_id={data['msg4']['id']}", headers=admin
    ).json()
    return data


# ---------------------------------------------------------------- 接入方注册


def test_注册回执精确形状与键序_令牌仅注册时回显(seed):
    body = seed["ep1"]
    assert list(body.keys()) == ENDPOINT_KEY_ORDER + ["auth_token"]
    assert body == {
        "id": body["id"],
        "code": "CT_HIS",
        "name": "契约医院HIS",
        "system_type": "his",
        "system_type_name": "医院信息系统",
        "direction": "inbound",
        "direction_name": "入站",
        "active": True,
        "rate_limit_per_min": 60,
        "endpoint_url": "",
        "created_at": body["created_at"],
        "auth_token": body["auth_token"],
    }
    assert isinstance(body["created_at"], str) and isinstance(body["auth_token"], str)
    # 出站端点：投递地址回显、签名密钥不回显（连键都没有）
    ep2 = seed["ep2"]
    assert ep2["endpoint_url"] == "https://prov.example/ingest"
    assert ep2["direction"] == "outbound" and ep2["direction_name"] == "出站"
    assert "secret" not in ep2


def test_修改回执精确_不含令牌键(seed):
    body = seed["ep1_patched"]
    assert list(body.keys()) == ENDPOINT_KEY_ORDER
    expected = {k: v for k, v in seed["ep1"].items() if k != "auth_token"}
    assert body == {**expected, "name": "契约医院HIS改", "rate_limit_per_min": 99}


def test_轮换回执精确_新令牌尾键(seed):
    body = seed["ep2_rotated"]
    assert list(body.keys()) == ENDPOINT_KEY_ORDER + ["auth_token"]
    assert body == {
        **{k: v for k, v in seed["ep2"].items() if k != "auth_token"},
        "auth_token": body["auth_token"],
    }
    assert body["auth_token"] != seed["ep2"]["auth_token"]


def test_接入方列表与回执同形并支持过滤(client, admin, seed):
    rows = client.get("/api/esb/endpoints", headers=admin).json()
    assert list(rows[0].keys()) == ENDPOINT_KEY_ORDER
    # 按 code 升序：CT_HIS（已改名）在前，CT_PROV 在后
    assert rows == [
        seed["ep1_patched"],
        {k: v for k, v in seed["ep2"].items() if k != "auth_token"},
    ]
    assert client.get("/api/esb/endpoints?system_type=provincial", headers=admin).json() == [
        rows[1]
    ]
    assert client.get("/api/esb/endpoints?active=true", headers=admin).json() == rows


# ---------------------------------------------------------------- 消息队列


def test_入队回执精确形状与键序_载荷原样透传(seed):
    body = seed["msg1"]
    assert list(body.keys()) == MESSAGE_KEY_ORDER
    assert body == {
        "id": body["id"],
        "endpoint_id": seed["ep1"]["id"],
        "endpoint_code": "CT_HIS",
        "msg_type": "generic",
        "payload": PAYLOAD_1,
        "status": "queued",
        "status_name": "待处理",
        "retry_count": 0,
        "max_retries": 3,
        "last_error": "",
        "next_retry_at": None,
        "created_at": body["created_at"],
        "updated_at": body["updated_at"],
    }
    assert isinstance(body["created_at"], str) and isinstance(body["updated_at"], str)
    # 载荷嵌套与非 ASCII 键逐项原样（宽 dict 契约不许滤键、不许改值类型）
    assert body["payload"]["nested"] == {"x": [1, 2], "flag": True}
    assert body["payload"]["金额"] == 12.5


def test_消费成功回执_消息键加尾键detail(seed):
    body = seed["msg1_done"]
    assert list(body.keys()) == MESSAGE_KEY_ORDER + ["detail"]
    assert body == {
        **seed["msg1"],
        "status": "succeeded",
        "status_name": "成功",
        "updated_at": body["updated_at"],
        "detail": "透传消息已处理（4 个字段）",
    }
    assert body["updated_at"] != seed["msg1"]["updated_at"]


def test_消费失败与死信回执精确(seed):
    failed = seed["msg4_failed"]
    assert list(failed.keys()) == MESSAGE_KEY_ORDER + ["detail"]
    assert failed == {
        **seed["msg4"],
        "status": "failed",
        "status_name": "失败待重试",
        "retry_count": 1,
        "last_error": "消息载荷为空，无法处理",
        "next_retry_at": failed["next_retry_at"],
        "updated_at": failed["updated_at"],
        "detail": "消息载荷为空，无法处理",
    }
    # failed 分支 next_retry_at 是 ISO 字符串；dead 分支回到 null
    assert isinstance(failed["next_retry_at"], str)
    dead = seed["msg2_dead"]
    assert dead == {
        **seed["msg2"],
        "status": "dead",
        "status_name": "死信",
        "retry_count": 1,
        "last_error": "消息载荷为空，无法处理",
        "next_retry_at": None,
        "updated_at": dead["updated_at"],
        "detail": "消息载荷为空，无法处理",
    }


def test_消息列表与回执同形_分页头与过滤(client, admin, seed):
    resp = client.get(
        f"/api/esb/messages?endpoint_id={seed['ep1']['id']}&limit=2", headers=admin
    )
    assert resp.headers["X-Total-Count"] == "5"
    rows = resp.json()
    assert len(rows) == 2 and list(rows[0].keys()) == MESSAGE_KEY_ORDER
    # id 倒序：msg5 从未被消费，列表行与入队回执逐键相等
    assert rows[0] == seed["msg5"]
    # msg4 又经编排失败一次：读列表时以最新状态呈现
    assert rows[1] == {
        **seed["msg4"],
        "status": "failed",
        "status_name": "失败待重试",
        "retry_count": 2,
        "last_error": "第 1 步（validate）失败：必填字段缺失：must_have",
        "next_retry_at": rows[1]["next_retry_at"],
        "updated_at": rows[1]["updated_at"],
    }
    queued = client.get(
        f"/api/esb/messages?endpoint_id={seed['ep1']['id']}&status=queued", headers=admin
    ).json()
    assert queued == [seed["msg5"]]
    by_type = client.get("/api/esb/messages?msg_type=report", headers=admin).json()
    assert by_type == [seed["msg6"]]


# ---------------------------------------------------------------- 流程编排


def test_建流程回执精确_步骤自定义键原样透传(seed):
    body = seed["flow1"]
    assert list(body.keys()) == FLOW_KEY_ORDER
    assert body == {
        "id": body["id"],
        "code": "CT_FLOW",
        "name": "契约编排",
        "steps": FLOW_STEPS,
        "step_count": 1,
        "active": True,
        "created_at": body["created_at"],
    }
    # 步骤 dict 的自定义键 note 必须原样保留——契约滤掉它即破坏字节
    assert body["steps"][0]["note"] == "自定义键要原样透传"


def test_改流程回执与列表同形(client, admin, seed):
    assert seed["flow1_patched"] == {**seed["flow1"], "name": "契约编排改名"}
    rows = client.get("/api/esb/flows", headers=admin).json()
    # 按 code 升序：CT_FLOW / CT_FLOW_BAD
    assert rows == [seed["flow1_patched"], seed["flow2"]]
    assert client.get("/api/esb/flows?active=true", headers=admin).json() == rows


def test_编排执行回执精确_成功分支(seed):
    body = seed["run1"]
    assert list(body.keys()) == [
        "id", "flow_code", "message_id", "status", "step_results",
        "error", "message_status", "retry_count",
    ]
    assert list(body["step_results"][0].keys()) == STEP_RESULT_KEY_ORDER
    assert body == {
        "id": body["id"],
        "flow_code": "CT_FLOW",
        "message_id": seed["msg3"]["id"],
        "status": "succeeded",
        "step_results": [
            {"step": 1, "type": "validate", "status": "succeeded", "detail": "校验通过（1 项必填）"}
        ],
        "error": "",
        "message_status": "succeeded",
        "retry_count": 0,
    }


def test_编排执行回执精确_失败分支(seed):
    body = seed["run2"]
    assert body == {
        "id": body["id"],
        "flow_code": "CT_FLOW_BAD",
        "message_id": seed["msg4"]["id"],
        "status": "failed",
        "step_results": [
            {"step": 1, "type": "validate", "status": "failed", "detail": "必填字段缺失：must_have"}
        ],
        "error": "必填字段缺失：must_have",
        "message_status": "failed",
        "retry_count": 2,
    }


def test_执行记录列表精确形状与过滤(client, admin, seed):
    rows = client.get("/api/esb/flow-runs", headers=admin).json()
    assert [list(r.keys()) for r in rows] == [
        ["id", "flow_id", "flow_code", "message_id", "status", "step_results", "error", "created_at"]
    ] * 2
    run1_row = {
        "id": seed["run1"]["id"],
        "flow_id": seed["flow1"]["id"],
        "flow_code": "CT_FLOW",
        "message_id": seed["msg3"]["id"],
        "status": "succeeded",
        "step_results": seed["run1"]["step_results"],
        "error": "",
        "created_at": rows[1]["created_at"],
    }
    run2_row = {
        "id": seed["run2"]["id"],
        "flow_id": seed["flow2"]["id"],
        "flow_code": "CT_FLOW_BAD",
        "message_id": seed["msg4"]["id"],
        "status": "failed",
        "step_results": seed["run2"]["step_results"],
        "error": "必填字段缺失：must_have",
        "created_at": rows[0]["created_at"],
    }
    assert rows == [run2_row, run1_row]  # id 倒序
    assert client.get("/api/esb/flow-runs?status=failed", headers=admin).json() == [run2_row]
    assert client.get(
        f"/api/esb/flow-runs?flow_id={seed['flow1']['id']}", headers=admin
    ).json() == [run1_row]
    assert client.get(
        f"/api/esb/flow-runs?message_id={seed['msg4']['id']}", headers=admin
    ).json() == [run2_row]


# ---------------------------------------------------------------- 统计看板


def test_统计精确_int与float之别(client, admin, seed):
    body = client.get("/api/esb/stats", headers=admin).json()
    assert list(body.keys()) == ["totals", "by_endpoint"]
    assert list(body["totals"].keys()) == [
        "total", "succeeded", "dead", "backlog",
        "success_rate_pct", "failure_rate_pct", "endpoints", "flows",
    ]
    assert [list(r.keys()) for r in body["by_endpoint"]] == [
        [
            "endpoint_id", "endpoint_code", "endpoint_name", "total", "succeeded",
            "dead", "queued", "failed", "backlog", "success_rate_pct", "failure_rate_pct",
        ]
    ] * 2
    # ep1：succeeded 2（msg1 消费 + msg3 编排）· dead 1 · failed 1 · queued 1
    # 已终结 3 条中成功 2 → 66.67%；ep2：仅 1 条 queued，rate 走 0.0 兜底字面量分支
    assert body == {
        "totals": {
            "total": 6, "succeeded": 2, "dead": 1, "backlog": 3,
            "success_rate_pct": 66.67, "failure_rate_pct": 33.33,
            "endpoints": 2, "flows": 2,
        },
        "by_endpoint": [
            {
                "endpoint_id": seed["ep1"]["id"], "endpoint_code": "CT_HIS",
                "endpoint_name": "契约医院HIS改", "total": 5, "succeeded": 2,
                "dead": 1, "queued": 1, "failed": 1, "backlog": 2,
                "success_rate_pct": 66.67, "failure_rate_pct": 33.33,
            },
            {
                "endpoint_id": seed["ep2"]["id"], "endpoint_code": "CT_PROV",
                "endpoint_name": "契约省平台", "total": 1, "succeeded": 0,
                "dead": 0, "queued": 1, "failed": 0, "backlog": 1,
                "success_rate_pct": 0.0, "failure_rate_pct": 0.0,
            },
        ],
    }
    totals = body["totals"]
    # 计数恒 int（声明成 float 会把 6 变 6.0，即改字节）；比率恒 float（0.0 兜底也是）
    for key in ("total", "succeeded", "dead", "backlog", "endpoints", "flows"):
        assert type(totals[key]) is int, key
    assert isinstance(totals["success_rate_pct"], float)
    assert isinstance(body["by_endpoint"][1]["success_rate_pct"], float)
    assert type(body["by_endpoint"][0]["total"]) is int
