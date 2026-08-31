"""药事监测 `/api/medication` 4 个未治理端点的**特征化网 + 响应契约**。

套路同 `test_billing_contract.py`：先钉住**当前**响应的完整 JSON（dict 相等）
与键序 → 再加 `response_model` → 加完逐字节不变（CLAUDE.md §7/§11）。
已治理的 4 个端点（shortages 登记/列表/流转/结案）不在此列。

本簇的建模判断（都以此处的精确断言为依据）：

- **`fulfillment_rate_pct` 是「键恒在值可空」→ `float | None`**：无可判定登记时
  返回 null（这是接口自己在 caliber 里写明的口径），有分母时是真除法
  `round(x*100/settled, 2)` 恒 float——两条分支各钉一遍，不是条件键，
  不需要 exclude_unset。
- **`by_status` 是宽键窄值 `dict[str, int]`**：键面由数据里出现过的状态决定
  （metrics.by_level 先例），值恒为 COUNT。
- **`max_daily_dose` 恒 float**：`daily_dose` 是 Float 列，整数入参 5/10 读回
  5.0/10.0，`max(0.0, …)` 也不改型——声明 float 才是原样（与 Money 相反，
  判据是列类型）。
- **计数恒 int**：times/rx_count/patient_count/low_stock_orgs/open_shortages/
  total 全是 COUNT 或 `+= 1` 累加，声明成 float 即改字节。
- `supply-risk` 的 `open_shortages` 口径是 `status != "delivered"`——已取药/
  未取药/已取消的登记**也计入**（当前实现如此，契约照抄现状不改行为）；
  仅缺药登记出现的药品 `drug_name` 是空串（不回填库存表的名字）。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app

STATS_KEYS = ["by_status", "in_transit", "collected", "no_show", "fulfillment_rate_pct", "caliber"]
CALIBER = (
    "履约率分母只含已判定取药与否的登记（collected + no_show），"
    "在途与已取消不计；无可判定登记时返回 null 而非 0"
)
PROFILE_KEYS = ["patient_id", "distinct_drugs", "polypharmacy_warning", "drugs"]
PROFILE_DRUG_KEYS = ["drug_code", "drug_name", "times", "max_daily_dose"]
USAGE_KEYS = ["drug_code", "drug_name", "rx_count", "patient_count"]
RISK_KEYS = ["total", "risks"]
RISK_ROW_KEYS = ["drug_code", "drug_name", "low_stock_orgs", "open_shortages", "risk_level"]


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
    """一次种完全部场景，测试只做断言。

    缺药登记终态铺满五种状态（collected/no_show/cancelled/purchasing/registered），
    药品编码的分布刻意让 supply-risk 三行各有来历：CT-INS 库存告警+登记（高风险）、
    CT-AML 仅登记×2（中风险，drug_name 空串）、CT-GAP 仅登记×2（中风险）。
    处方混铺整数剂量（5/10 → 读回 float）与小数剂量（1.5）。
    """
    data: dict = {}
    org = client.post(
        "/api/organizations",
        json={"name": "契约药事医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    data["org"] = org
    for username, role in [("mdct_op", "operator"), ("mdct_doc", "doctor")]:
        client.post(
            "/api/users",
            json={"username": username, "password": "pass123456", "role": role, "org_id": org["id"]},
            headers=admin,
        )
    data["operator"] = login(client, "mdct_op", "pass123456")
    data["doctor"] = login(client, "mdct_doc", "pass123456")
    data["p1"] = client.post(
        "/api/patients",
        json={"name": "契约药事患者一", "id_card": "330881199001018831"},
        headers=admin,
    ).json()
    data["p2"] = client.post(
        "/api/patients",
        json={"name": "契约药事患者二", "id_card": "330881199001018832"},
        headers=admin,
    ).json()

    # 空库分支先取快照
    data["risk_empty"] = client.get("/api/medication/supply-risk", headers=data["operator"]).json()

    def shortage(drug_code, drug_name, patient_id=None):
        payload = {"org_id": org["id"], "drug_code": drug_code, "drug_name": drug_name}
        if patient_id is not None:
            payload["patient_id"] = patient_id
        resp = client.post("/api/medication/shortages", json=payload, headers=data["operator"])
        assert resp.status_code == 201, resp.text
        return resp.json()

    def advance(sid, times):
        for _ in range(times):
            assert client.post(
                f"/api/medication/shortages/{sid}/advance", headers=data["operator"]
            ).status_code == 200

    def close(sid, result, reason=""):
        resp = client.post(
            f"/api/medication/shortages/{sid}/close",
            json={"result": result, "reason": reason},
            headers=data["operator"],
        )
        assert resp.status_code == 200, resp.text
        return resp.json()

    s1 = shortage("CT-INS", "胰岛素(契约)", patient_id=data["p1"]["id"])
    s2 = shortage("CT-GAP", "缺供药甲(契约)")
    s3 = shortage("CT-GAP", "缺供药甲(契约)")
    # 全部还在 registered：无已判定登记 → 履约率 null 分支
    data["stats_early"] = client.get("/api/medication/shortages/stats", headers=data["operator"]).json()
    advance(s1["id"], 2)
    close(s1["id"], "collected")
    advance(s2["id"], 1)
    s4 = shortage("CT-AML", "氨氯地平(契约)")
    advance(s4["id"], 2)
    close(s4["id"], "no_show", reason="多次联系不来取")
    s5 = shortage("CT-AML", "氨氯地平(契约)")
    close(s5["id"], "cancelled", reason="药源已解决")
    data["shortages"] = [s1, s2, s3, s4, s5]

    # 处方（通过审方即计入画像/用药地图；CT-* 编码无规则 → auto_passed）
    def rx(patient, items):
        resp = client.post(
            "/api/prescriptions",
            json={"patient_id": patient["id"], "org_id": org["id"],
                  "diagnosis_name": "高血压", "items": items},
            headers=data["doctor"],
        )
        assert resp.status_code == 201, resp.text
        return resp.json()

    rx(data["p1"], [
        {"drug_code": "CT-AML", "drug_name": "氨氯地平(契约)", "daily_dose": 5, "days": 30},
        {"drug_code": "CT-MET", "drug_name": "二甲双胍(契约)", "daily_dose": 1.5, "days": 30},
    ])
    rx(data["p1"], [
        {"drug_code": "CT-AML", "drug_name": "氨氯地平(契约)", "daily_dose": 10, "days": 7},
    ])
    rx(data["p2"], [
        {"drug_code": f"CT-D{i}", "drug_name": f"契约药{i}", "daily_dose": 1, "days": 7}
        for i in range(1, 6)
    ])

    # 库存：CT-INS 低于阈值；CT-AML 充足（不该进 low_stock）；阈值 0 不参与预警
    for code, name, qty, threshold in [
        ("CT-INS", "胰岛素(契约)", 5, 50),
        ("CT-AML", "氨氯地平(契约)", 100, 10),
        ("CT-ZERO", "无阈值药(契约)", 0, 0),
    ]:
        assert client.post(
            "/api/pharmacy/stocks",
            json={"org_id": org["id"], "drug_code": code, "drug_name": name,
                  "quantity": qty, "threshold": threshold},
            headers=admin,
        ).status_code == 200
    return data


# ---------------------------------------------------------------- 缺药登记统计


def test_缺药统计精确_无可判定登记时履约率为null(seed):
    body = seed["stats_early"]
    assert list(body.keys()) == STATS_KEYS
    assert body == {
        "by_status": {"registered": 3},
        "in_transit": 3,
        "collected": 0,
        "no_show": 0,
        "fulfillment_rate_pct": None,
        "caliber": CALIBER,
    }
    assert type(body["in_transit"]) is int and type(body["by_status"]["registered"]) is int


def test_缺药统计精确_五状态齐备与履约率(client, seed):
    body = client.get("/api/medication/shortages/stats", headers=seed["operator"]).json()
    assert list(body.keys()) == STATS_KEYS
    # group_by(status) 按状态字典序出键
    assert list(body["by_status"].keys()) == [
        "cancelled", "collected", "no_show", "purchasing", "registered",
    ]
    assert body == {
        "by_status": {"cancelled": 1, "collected": 1, "no_show": 1,
                      "purchasing": 1, "registered": 1},
        "in_transit": 2,
        "collected": 1,
        "no_show": 1,
        "fulfillment_rate_pct": 50.0,
        "caliber": CALIBER,
    }
    # 履约率是真除法：恒 float（声明 int 或去掉 None 分支都改字节）
    assert isinstance(body["fulfillment_rate_pct"], float)


# ---------------------------------------------------------------- 居民用药画像


def test_用药画像精确_键序与Float剂量(client, admin, seed):
    body = client.get(f"/api/medication/profile/{seed['p1']['id']}", headers=admin).json()
    assert list(body.keys()) == PROFILE_KEYS
    assert [list(d.keys()) for d in body["drugs"]] == [PROFILE_DRUG_KEYS] * 2
    assert body == {
        "patient_id": seed["p1"]["id"],
        "distinct_drugs": 2,
        "polypharmacy_warning": False,
        "drugs": [
            {"drug_code": "CT-AML", "drug_name": "氨氯地平(契约)",
             "times": 2, "max_daily_dose": 10.0},
            {"drug_code": "CT-MET", "drug_name": "二甲双胍(契约)",
             "times": 1, "max_daily_dose": 1.5},
        ],
    }
    # Float 列：整数入参 5/10 读回就是 float，10 必须以 10.0 出参（与 Money 相反）
    assert type(body["drugs"][0]["max_daily_dose"]) is float
    assert type(body["drugs"][0]["times"]) is int


def test_用药画像精确_多重用药预警分支(client, admin, seed):
    body = client.get(f"/api/medication/profile/{seed['p2']['id']}", headers=admin).json()
    assert body == {
        "patient_id": seed["p2"]["id"],
        "distinct_drugs": 5,
        "polypharmacy_warning": True,
        "drugs": [
            {"drug_code": f"CT-D{i}", "drug_name": f"契约药{i}", "times": 1, "max_daily_dose": 1.0}
            for i in range(1, 6)
        ],
    }


# ---------------------------------------------------------------- 全县用药地图


def test_用药地图精确_排名与计数类型(client, admin, seed):
    rows = client.get("/api/medication/usage-stats", headers=admin).json()
    assert [list(r.keys()) for r in rows] == [USAGE_KEYS] * 7
    # 处方次数第一名：CT-AML（2 张方、1 名患者）
    assert rows[0] == {"drug_code": "CT-AML", "drug_name": "氨氯地平(契约)",
                       "rx_count": 2, "patient_count": 1}
    assert type(rows[0]["rx_count"]) is int and type(rows[0]["patient_count"]) is int
    # 其余 6 项都是 1 张方（并列名次不钉先后，按编码归一后精确比对）
    assert sorted(rows[1:], key=lambda r: r["drug_code"]) == [
        {"drug_code": f"CT-D{i}", "drug_name": f"契约药{i}", "rx_count": 1, "patient_count": 1}
        for i in range(1, 6)
    ] + [
        {"drug_code": "CT-MET", "drug_name": "二甲双胍(契约)", "rx_count": 1, "patient_count": 1},
    ]


# ---------------------------------------------------------------- 供应风险评估


def test_供应风险精确_空库分支(seed):
    assert seed["risk_empty"] == {"total": 0, "risks": []}


def test_供应风险精确_三行各有来历(client, seed):
    body = client.get("/api/medication/supply-risk", headers=seed["operator"]).json()
    assert list(body.keys()) == RISK_KEYS
    assert [list(r.keys()) for r in body["risks"]] == [RISK_ROW_KEYS] * 3
    assert body == {
        "total": 3,
        "risks": [
            # 库存告警 + 未结案登记（collected 也算 open：口径是 status != delivered）
            {"drug_code": "CT-INS", "drug_name": "胰岛素(契约)",
             "low_stock_orgs": 1, "open_shortages": 1, "risk_level": "high"},
            # 仅登记出现的药品：drug_name 是空串（不回填库存名），中风险，按编码排序
            {"drug_code": "CT-AML", "drug_name": "",
             "low_stock_orgs": 0, "open_shortages": 2, "risk_level": "medium"},
            {"drug_code": "CT-GAP", "drug_name": "",
             "low_stock_orgs": 0, "open_shortages": 2, "risk_level": "medium"},
        ],
    }
    assert type(body["total"]) is int
    assert type(body["risks"][0]["low_stock_orgs"]) is int
    assert type(body["risks"][0]["open_shortages"]) is int


# ---------------------------------------------------------------- 错误体


def test_各类错误体都只有detail(client, admin, seed):
    cases = [
        client.get("/api/medication/profile/999999", headers=admin),  # 404
        client.post("/api/medication/shortages",
                    json={"org_id": 999999, "drug_code": "X", "drug_name": "无"},
                    headers=admin),  # 404
        client.post(f"/api/medication/shortages/{seed['shortages'][0]['id']}/advance",
                    headers=seed["operator"]),  # 终态 409
        client.post(f"/api/medication/shortages/{seed['shortages'][0]['id']}/close",
                    json={"result": "cancelled"},
                    headers=seed["operator"]),  # 已结案 409
        client.post(f"/api/medication/shortages/{seed['shortages'][2]['id']}/close",
                    json={"result": "collected"},
                    headers=seed["operator"]),  # 未配送判取药 409
    ]
    assert [r.status_code for r in cases] == [404, 404, 409, 409, 409]
    for r in cases:
        assert set(r.json()) == {"detail"}
