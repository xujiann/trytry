"""上门服务调度 `/api/homevisits` 全部 6 个端点的**特征化网 + 响应契约**。

套路同 test_billing_contract.py / test_maternal_contract.py：先钉住**当前**
响应的完整 JSON（dict 相等）与键序 → 再加 `response_model` → 加完逐字节不变
（CLAUDE.md §7/§11）。

本簇的建模判断（都以此处的精确断言为依据）：

- 本簇**没有 Money/Float 列**；唯一比率 `contract_linked_ratio_pct` 恒 float
  （`round(x*100.0/total,2)` 与兜底字面量 `0.0` 两条产地都是浮点）。
- 工单回执与列表行**同形**（`_visit_out` 唯一产地，14 键），四个动作回执共用
  一个模型；`contract_id` 与 `dispatched_at`/`completed_at` 是「键恒在值可空」
  → `X | None`，不是条件键，无需 exclude_unset（时间戳出参是 isoformat 字符串）。
- `by_status` 键是状态码（随数据变）、值是计数 → `dict[str, int]`。
- 列表走 `deps.paginate`：limit/offset + X-Total-Count 头照现状钉住。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app

VISIT_KEYS = [
    "id", "patient_id", "contract_id", "org_id", "service_type", "service_type_name",
    "demand", "address", "expect_date", "status", "assignee_name", "dispatched_at",
    "service_note", "completed_at",
]
STATS_KEYS = ["total", "by_status", "contract_linked", "contract_linked_ratio_pct"]


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


def test_上门服务统计_零态精确(client, admin):
    """放在最前：此刻还没有任何工单，比率的兜底 0.0 分支才钉得住。"""
    resp = client.get("/api/homevisits/stats", headers=admin)
    body = resp.json()
    assert list(body.keys()) == STATS_KEYS
    assert body == {"total": 0, "by_status": {}, "contract_linked": 0,
                    "contract_linked_ratio_pct": 0.0}
    assert isinstance(body["contract_linked_ratio_pct"], float)


@pytest.fixture(scope="module")
def seed(client, admin):
    """三张工单走三条终线：hv1 自动关联签约并 派单→完成；hv2 无签约、取消；
    hv3 显式指定签约、停在待派单。另备一份已解约签约钉 409。"""
    data: dict = {}
    org = client.post(
        "/api/organizations",
        json={"name": "契约上门医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    data["org"] = org
    for username, role in [("hvct_doc", "doctor"), ("hvct_op", "operator"),
                           ("hvct_ph", "public_health")]:
        client.post(
            "/api/users",
            json={"username": username, "password": "pass123456", "role": role, "org_id": org["id"]},
            headers=admin,
        )
    data["doctor"] = login(client, "hvct_doc", "pass123456")
    data["operator"] = login(client, "hvct_op", "pass123456")
    data["ph"] = login(client, "hvct_ph", "pass123456")
    data["patients"] = [
        client.post(
            "/api/patients",
            json={"name": f"契约上门患者{i}", "id_card": f"33088119900101{8901 + i:04d}"},
            headers=admin,
        ).json()
        for i in range(3)
    ]
    resp = client.post(
        "/api/contracts",
        json={"patient_id": data["patients"][0]["id"], "org_id": org["id"],
              "doctor_name": "李家医", "package": "premium", "signed_date": "2026-01-05"},
        headers=data["doctor"],
    )
    assert resp.status_code == 201, resp.text
    data["contract"] = resp.json()
    # 已解约的签约：显式指定它派单应 409
    terminated = client.post(
        "/api/contracts",
        json={"patient_id": data["patients"][1]["id"], "org_id": org["id"],
              "doctor_name": "张家医", "package": "basic", "signed_date": "2026-01-06"},
        headers=data["doctor"],
    ).json()
    client.post(f"/api/contracts/{terminated['id']}/terminate", headers=data["doctor"])
    data["terminated_contract"] = terminated

    resp = client.post(
        "/api/homevisits",
        json={"patient_id": data["patients"][0]["id"], "org_id": org["id"],
              "service_type": "nursing", "demand": "留置导尿管更换",
              "address": "契约村 12 号", "expect_date": "2026-09-10"},
        headers=data["operator"],
    )
    assert resp.status_code == 201, resp.text
    data["hv1"] = resp.json()
    data["hv1_dispatched"] = client.post(
        f"/api/homevisits/{data['hv1']['id']}/dispatch",
        json={"assignee_name": "赵护士"}, headers=data["operator"],
    ).json()
    data["hv1_completed"] = client.post(
        f"/api/homevisits/{data['hv1']['id']}/complete",
        json={"service_note": "已更换导尿管，指导家属护理"}, headers=data["operator"],
    ).json()
    data["hv2"] = client.post(
        "/api/homevisits",
        json={"patient_id": data["patients"][1]["id"], "org_id": org["id"],
              "service_type": "doctor"},
        headers=data["ph"],
    ).json()
    data["hv2_cancelled"] = client.post(
        f"/api/homevisits/{data['hv2']['id']}/cancel", headers=data["doctor"]
    ).json()
    data["hv3"] = client.post(
        "/api/homevisits",
        json={"patient_id": data["patients"][0]["id"], "org_id": org["id"],
              "service_type": "sampling", "contract_id": data["contract"]["id"],
              "expect_date": "2026-09-12"},
        headers=data["doctor"],
    ).json()
    return data


def test_申请回执精确_自动关联签约(seed):
    body = seed["hv1"]
    assert list(body.keys()) == VISIT_KEYS
    assert body == {
        "id": body["id"],
        "patient_id": seed["patients"][0]["id"],
        "contract_id": seed["contract"]["id"],  # 未传 contract_id：按患者+机构自动关联
        "org_id": seed["org"]["id"],
        "service_type": "nursing",
        "service_type_name": "上门护理",
        "demand": "留置导尿管更换",
        "address": "契约村 12 号",
        "expect_date": "2026-09-10",
        "status": "applied",
        "assignee_name": "",
        "dispatched_at": None,
        "service_note": "",
        "completed_at": None,
    }
    # 无签约患者：contract_id 是「键恒在值可空」——键在、值 null，不是键消失
    assert seed["hv2"]["contract_id"] is None and seed["hv2"]["service_type_name"] == "上门诊疗"
    assert seed["hv2"]["demand"] == "" and seed["hv2"]["expect_date"] == ""
    # 显式指定签约
    assert seed["hv3"]["contract_id"] == seed["contract"]["id"]
    assert seed["hv3"]["service_type_name"] == "上门采样"


def test_派单完成取消回执逐步精确(seed):
    dispatched = seed["hv1_dispatched"]
    assert list(dispatched.keys()) == VISIT_KEYS
    assert dispatched == {
        **seed["hv1"], "status": "dispatched", "assignee_name": "赵护士",
        "dispatched_at": dispatched["dispatched_at"],
    }
    assert isinstance(dispatched["dispatched_at"], str)  # isoformat 字符串，不是时间对象
    completed = seed["hv1_completed"]
    assert completed == {
        **dispatched, "status": "completed", "service_note": "已更换导尿管，指导家属护理",
        "completed_at": completed["completed_at"],
    }
    assert isinstance(completed["completed_at"], str)
    assert seed["hv2_cancelled"] == {**seed["hv2"], "status": "cancelled"}


def test_工单列表与回执同形_分页与过滤(client, admin, seed):
    resp = client.get("/api/homevisits", headers=admin)
    rows = resp.json()
    assert resp.headers["X-Total-Count"] == "3"
    assert [list(r.keys()) for r in rows] == [VISIT_KEYS] * 3
    assert rows == [seed["hv3"], seed["hv2_cancelled"], seed["hv1_completed"]]  # id 倒序
    assert client.get("/api/homevisits?status=completed", headers=admin).json() == [
        seed["hv1_completed"]
    ]
    assert client.get(
        f"/api/homevisits?patient_id={seed['patients'][0]['id']}&org_id={seed['org']['id']}",
        headers=admin,
    ).json() == [seed["hv3"], seed["hv1_completed"]]
    paged = client.get("/api/homevisits?offset=1&limit=1", headers=admin)
    assert paged.headers["X-Total-Count"] == "3"
    assert paged.json() == [seed["hv2_cancelled"]]


def test_上门服务统计精确_比率恒float(client, admin, seed):
    resp = client.get("/api/homevisits/stats", headers=admin)
    body = resp.json()
    assert list(body.keys()) == STATS_KEYS
    assert body == {
        "total": 3,
        "by_status": {"applied": 1, "cancelled": 1, "completed": 1},
        "contract_linked": 2,
        "contract_linked_ratio_pct": 66.67,
    }
    assert isinstance(body["contract_linked_ratio_pct"], float)
    assert type(body["contract_linked"]) is int and type(body["by_status"]["applied"]) is int


def test_各类错误体都只有detail(client, admin, seed):
    org_id = seed["org"]["id"]
    cases = [
        client.post("/api/homevisits",
                    json={"patient_id": 999999, "org_id": org_id, "service_type": "nursing"},
                    headers=seed["operator"]),  # 患者不存在 404
        client.post("/api/homevisits",
                    json={"patient_id": seed["patients"][0]["id"], "org_id": org_id,
                          "service_type": "nursing", "contract_id": 999999},
                    headers=seed["operator"]),  # 签约不存在 404
        client.post("/api/homevisits",
                    json={"patient_id": seed["patients"][0]["id"], "org_id": org_id,
                          "service_type": "nursing",
                          "contract_id": seed["terminated_contract"]["id"]},
                    headers=seed["operator"]),  # 签约患者不一致 422
        client.post("/api/homevisits",
                    json={"patient_id": seed["patients"][1]["id"], "org_id": org_id,
                          "service_type": "rehab",
                          "contract_id": seed["terminated_contract"]["id"]},
                    headers=seed["operator"]),  # 已解约 409
        client.post("/api/homevisits/999999/dispatch",
                    json={"assignee_name": "无人"}, headers=seed["operator"]),  # 404
        client.post(f"/api/homevisits/{seed['hv1']['id']}/dispatch",
                    json={"assignee_name": "重复"}, headers=seed["operator"]),  # 已完成 409
        client.post("/api/homevisits/999999/complete",
                    json={"service_note": "无单"}, headers=seed["operator"]),  # 404
        client.post(f"/api/homevisits/{seed['hv3']['id']}/complete",
                    json={"service_note": "未派单"}, headers=seed["operator"]),  # 409
        client.post("/api/homevisits/999999/cancel", headers=seed["operator"]),  # 404
        client.post(f"/api/homevisits/{seed['hv1']['id']}/cancel",
                    headers=seed["operator"]),  # 已完成 409
    ]
    assert [r.status_code for r in cases] == [404, 404, 422, 409, 404, 409, 404, 409, 404, 409]
    for r in cases:
        assert set(r.json()) == {"detail"}
