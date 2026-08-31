"""病理标本 `/api/pathology` 全部 6 个端点的**特征化网 + 响应契约**。

套路同 test_billing_contract.py / test_maternal_contract.py：先钉住**当前**
响应的完整 JSON（dict 相等）与键序 → 再加 `response_model` → 加完逐字节不变
（CLAUDE.md §7/§11）。

本簇的建模判断（都以此处的精确断言为依据）：

- 本簇**没有 Money/Float 列**，数值全是 Integer；两个比率/均值是**真除法产地**：
  `reject_rate_pct` 与 `avg_minutes` 有值时恒 float（`round(x*100/total,2)` /
  `round(sum/len,1)` 都是真除法），**无标本/无实测时是 None**——两个都是
  「键恒在值可空」→ `float | None`，不是条件键，无需 exclude_unset。
  零态在种任何标本前单独钉住（None 分支）。
- `cold_ischemia_minutes` 同理是恒在键：时间未填全或倒序（fixed < excised）
  时为 null，填全时是 int（分钟数整除）→ `int | None`。
- 标本回执与列表行**同形**（`_out` 唯一产地，15 键），新建/核收/拒收/推进
  四个动作回执共用一个模型。
- `by_status` 的键是状态码（随数据变），值恒为 {count, name} 两键子形状
  → `dict[str, 子模型]`；`reject_reason_options` 是固定字符串清单。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app

SPECIMEN_KEYS = [
    "id", "request_id", "specimen_no", "site", "excised_at", "fixed_at", "fixative",
    "cold_ischemia_minutes", "status", "status_name", "reject_reason", "received_by",
    "block_count", "slide_count", "note",
]
STATS_KEYS = [
    "total", "by_status", "rejected", "reject_rate_pct", "cold_ischemia",
    "reject_reason_options", "caliber",
]
COLD_KEYS = ["measured", "unmeasured", "avg_minutes", "over_60min"]
REJECT_REASONS = ["标本量不足", "未加固定液", "标识不清", "标本破损", "申请单信息不符"]
CALIBER = "冷缺血时间＝离体到固定；离体或固定时间未填的不参与均值，单列为 unmeasured"


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


def test_标本质控统计_零态精确(client, admin):
    """放在最前：此刻还没有任何标本，reject_rate_pct/avg_minutes 的 None 分支才钉得住。"""
    resp = client.get("/api/pathology/specimen-stats", headers=admin)
    body = resp.json()
    assert list(body.keys()) == STATS_KEYS
    assert list(body["cold_ischemia"].keys()) == COLD_KEYS
    assert body == {
        "total": 0,
        "by_status": {},
        "rejected": 0,
        "reject_rate_pct": None,
        "cold_ischemia": {"measured": 0, "unmeasured": 0, "avg_minutes": None, "over_60min": 0},
        "reject_reason_options": REJECT_REASONS,
        "caliber": CALIBER,
    }


@pytest.fixture(scope="module")
def seed(client, admin):
    """一次种完全部场景，测试只做断言（billing 契约网同款布局）。

    四个标本：s1 走完整链（核收→取材→制片→阅片，冷缺血 20 分钟）；
    s2 拒收（未加固定液）；s3 只记离体时间（unmeasured）；
    s4 时间倒序（fixed < excised，同样不拿来凑均值）。
    """
    data: dict = {}
    org = client.post(
        "/api/organizations",
        json={"name": "契约病理医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    for username, role in [("plct_doc", "doctor"), ("plct_op", "operator")]:
        client.post(
            "/api/users",
            json={"username": username, "password": "pass123456", "role": role, "org_id": org["id"]},
            headers=admin,
        )
    data["doctor"] = login(client, "plct_doc", "pass123456")
    data["operator"] = login(client, "plct_op", "pass123456")
    patient = client.post(
        "/api/patients",
        json={"name": "契约病理患者", "id_card": "330881199001017801"},
        headers=admin,
    ).json()
    data["request"] = client.post(
        "/api/exams",
        json={"patient_id": patient["id"], "from_org_id": org["id"], "center_type": "pathology",
              "item_code": "P001", "item_name": "组织病理学检查"},
        headers=admin,
    ).json()
    data["lab_request"] = client.post(
        "/api/exams",
        json={"patient_id": patient["id"], "from_org_id": org["id"], "center_type": "lab",
              "item_code": "L001", "item_name": "血常规"},
        headers=admin,
    ).json()

    def submit(payload):
        resp = client.post("/api/pathology/specimens", json=payload, headers=data["doctor"])
        assert resp.status_code == 201, resp.text
        return resp.json()

    rid = data["request"]["id"]
    data["s1"] = submit({"request_id": rid, "site": "胃窦", "excised_at": "2026-08-12T09:00:00",
                         "fixed_at": "2026-08-12T09:20:00", "fixative": "10%中性福尔马林",
                         "note": "两块组织"})
    data["s1_received"] = client.post(
        f"/api/pathology/specimens/{data['s1']['id']}/receive",
        json={"received_by": "病理技师甲"}, headers=data["operator"],
    ).json()
    data["s1_embedded"] = client.post(
        f"/api/pathology/specimens/{data['s1']['id']}/advance",
        json={"block_count": 3}, headers=data["operator"],
    ).json()
    data["s1_slided"] = client.post(
        f"/api/pathology/specimens/{data['s1']['id']}/advance",
        json={"slide_count": 6}, headers=data["operator"],
    ).json()
    data["s1_read"] = client.post(
        f"/api/pathology/specimens/{data['s1']['id']}/advance", json={}, headers=data["operator"]
    ).json()
    data["s2"] = submit({"request_id": rid})
    data["s2_rejected"] = client.post(
        f"/api/pathology/specimens/{data['s2']['id']}/reject",
        json={"reject_reason": "未加固定液"}, headers=data["operator"],
    ).json()
    data["s3"] = submit({"request_id": rid, "site": "只记离体", "excised_at": "2026-08-12T10:00:00"})
    data["s4"] = submit({"request_id": rid, "site": "倒序时间", "excised_at": "2026-08-12T11:00:00",
                         "fixed_at": "2026-08-12T10:00:00"})
    return data


def test_送检回执精确_键序与冷缺血分钟(seed):
    body = seed["s1"]
    assert list(body.keys()) == SPECIMEN_KEYS
    assert body == {
        "id": body["id"],
        "request_id": seed["request"]["id"],
        "specimen_no": "P000001",
        "site": "胃窦",
        "excised_at": "2026-08-12T09:00:00",
        "fixed_at": "2026-08-12T09:20:00",
        "fixative": "10%中性福尔马林",
        "cold_ischemia_minutes": 20,
        "status": "pending",
        "status_name": "待核收",
        "reject_reason": "",
        "received_by": "",
        "block_count": 0,
        "slide_count": 0,
        "note": "两块组织",
    }
    # 冷缺血分钟是 Integer 派生（秒差整除 60），不是 20.0
    assert type(body["cold_ischemia_minutes"]) is int
    # 未填/倒序两种不完整时间：键在、值为 null（不是键消失，也不拿当前时间凑数）
    assert seed["s2"]["cold_ischemia_minutes"] is None and seed["s2"]["site"] == ""
    assert seed["s3"]["cold_ischemia_minutes"] is None
    assert seed["s4"]["cold_ischemia_minutes"] is None


def test_核收与推进回执逐步精确(seed):
    assert seed["s1_received"] == {
        **seed["s1"], "status": "received", "status_name": "已核收", "received_by": "病理技师甲",
    }
    assert seed["s1_embedded"] == {
        **seed["s1_received"], "status": "embedded", "status_name": "已取材", "block_count": 3,
    }
    assert seed["s1_slided"] == {
        **seed["s1_embedded"], "status": "slided", "status_name": "已制片", "slide_count": 6,
    }
    assert seed["s1_read"] == {**seed["s1_slided"], "status": "read", "status_name": "已阅片"}
    assert list(seed["s1_read"].keys()) == SPECIMEN_KEYS
    assert type(seed["s1_embedded"]["block_count"]) is int


def test_拒收回执精确(seed):
    body = seed["s2_rejected"]
    assert list(body.keys()) == SPECIMEN_KEYS
    assert body == {
        **seed["s2"], "status": "rejected", "status_name": "已拒收", "reject_reason": "未加固定液",
    }


def test_标本列表与回执同形_过滤(client, admin, seed):
    rows = client.get("/api/pathology/specimens", headers=admin).json()
    assert [list(r.keys()) for r in rows] == [SPECIMEN_KEYS] * 4
    assert rows == [seed["s4"], seed["s3"], seed["s2_rejected"], seed["s1_read"]]  # id 倒序
    assert client.get(
        f"/api/pathology/specimens?request_id={seed['request']['id']}&status=pending",
        headers=admin,
    ).json() == [seed["s4"], seed["s3"]]
    assert client.get(
        "/api/pathology/specimens?status=read", headers=admin
    ).json() == [seed["s1_read"]]
    assert client.get("/api/pathology/specimens?request_id=999999", headers=admin).json() == []


def test_标本质控统计精确_比率恒float(client, admin, seed):
    resp = client.get("/api/pathology/specimen-stats", headers=admin)
    body = resp.json()
    assert list(body.keys()) == STATS_KEYS
    assert body == {
        "total": 4,
        "by_status": {
            "pending": {"count": 2, "name": "待核收"},
            "read": {"count": 1, "name": "已阅片"},
            "rejected": {"count": 1, "name": "已拒收"},
        },
        "rejected": 1,
        "reject_rate_pct": 25.0,
        # 只有 s1 填全且未倒序：均值只算它，s2/s3/s4 单列 unmeasured
        "cold_ischemia": {"measured": 1, "unmeasured": 3, "avg_minutes": 20.0, "over_60min": 0},
        "reject_reason_options": REJECT_REASONS,
        "caliber": CALIBER,
    }
    # 真除法产地：整值也是 25.0 / 20.0，声明成 int 会改字节
    assert isinstance(body["reject_rate_pct"], float)
    assert isinstance(body["cold_ischemia"]["avg_minutes"], float)
    assert type(body["by_status"]["pending"]["count"]) is int


def test_各类错误体都只有detail(client, admin, seed):
    cases = [
        client.post("/api/pathology/specimens",
                    json={"request_id": 999999}, headers=seed["doctor"]),  # 申请不存在 404
        client.post("/api/pathology/specimens",
                    json={"request_id": seed["lab_request"]["id"]}, headers=seed["doctor"]),  # 非病理 422
        client.post("/api/pathology/specimens/999999/receive",
                    json={"received_by": "无人"}, headers=seed["operator"]),  # 404
        client.post(f"/api/pathology/specimens/{seed['s1']['id']}/receive",
                    json={"received_by": "重复"}, headers=seed["operator"]),  # 非待核收 409
        client.post(f"/api/pathology/specimens/{seed['s1']['id']}/reject",
                    json={"reject_reason": "标本量不足"}, headers=seed["operator"]),  # 已推进 409
        client.post(f"/api/pathology/specimens/{seed['s3']['id']}/reject",
                    json={"reject_reason": "自由发挥的原因"}, headers=seed["operator"]),  # 非标准项 422
        client.post(f"/api/pathology/specimens/{seed['s3']['id']}/advance",
                    json={}, headers=seed["operator"]),  # 待核收未核收 409
        client.post(f"/api/pathology/specimens/{seed['s1']['id']}/advance",
                    json={}, headers=seed["operator"]),  # 已阅片 409
        client.post(f"/api/pathology/specimens/{seed['s2']['id']}/advance",
                    json={}, headers=seed["operator"]),  # 已拒收 409
    ]
    assert [r.status_code for r in cases] == [404, 422, 404, 409, 409, 422, 409, 409, 409]
    for r in cases:
        assert set(r.json()) == {"detail"}
