"""医保协同 `/api/insurance` 平台侧 5 个未治理端点的**特征化网 + 响应契约**。

套路同 `test_billing_contract.py`：先钉住**当前**响应的完整 JSON（dict 相等）
与键序 → 再加 `response_model` → 加完逐字节不变（CLAUDE.md §7/§11）。
已治理的 5 个端点（settlements×2 / special-diseases×3）不在此列。

本簇的建模判断（都以此处的精确断言为依据）：

- **`insurance_pay_total` 是 `int | float`**：Money 列之和经 `round(x, 2)`，
  整数金额（70+120+20=210）读回 `int`，声明成 float 会把「210 元」印成
  「210.0 元」；混入 0.5 后同一字段是 `210.5`（float）——种子里两种取值各钉一遍，
  空库分支走 `coalesce(…, 0.0)` 字面量，是 `0.0` 不是 `0`。
- **两个占比恒 float**：`part * 100.0 / whole` 真除法与兜底字面量 `0.0`
  两条产地全是浮点，声明 float 才是原样。
- 转诊证明/双通道三种回执都是**固定键集**（无条件键），不需要 exclude_unset；
  `review_comment` 是「键恒在值可空为空串」→ 声明 str，不是 str | None。
- 三种双通道回执**不同形**（申报 3 键带 drug_name / 审核 2 键 / 列表行 6 键），
  各建各的模型，不硬套继承。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app

FUND_KEYS = ["insurance_pay_total", "local_ratio_pct", "grassroots_ratio_pct"]
CERT_KEYS = ["cert_no", "referral_id"]
DUAL_APPLY_KEYS = ["id", "status", "drug_name"]
DUAL_REVIEW_KEYS = ["id", "status"]
DUAL_ROW_KEYS = ["id", "patient_id", "drug_name", "reason", "status", "review_comment"]


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

    结算金额刻意分两段：先全整数（基层 local 70 + 县级 remote 120 + 县级 local 20
    → 总额 210、local 90、基层 70，三个分量互不相同，占比断言才咬得住），
    基金监测取一次快照；再补一笔 0.5 的小数结算，让同一字段变 float。
    """
    data: dict = {}
    data["org_t"] = client.post(
        "/api/organizations",
        json={"name": "契约医保卫生院", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()
    data["org_c"] = client.post(
        "/api/organizations",
        json={"name": "契约医保县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    for username, role, org in [
        ("inct_op", "operator", data["org_t"]),
        ("inct_dir", "director", data["org_c"]),
        ("inct_doc", "doctor", data["org_c"]),
    ]:
        client.post(
            "/api/users",
            json={"username": username, "password": "pass123456", "role": role, "org_id": org["id"]},
            headers=admin,
        )
    data["operator"] = login(client, "inct_op", "pass123456")
    data["director"] = login(client, "inct_dir", "pass123456")
    data["doctor"] = login(client, "inct_doc", "pass123456")
    data["patient"] = client.post(
        "/api/patients",
        json={"name": "契约医保患者", "id_card": "330881199001018811"},
        headers=admin,
    ).json()

    # 空库分支先取快照，再种结算
    data["fund_empty"] = client.get("/api/insurance/fund-stats", headers=data["director"]).json()

    def settle(org, settle_type, total, ins, self_pay):
        resp = client.post(
            "/api/insurance/settlements",
            json={"patient_id": data["patient"]["id"], "org_id": org["id"],
                  "settle_type": settle_type, "total_amount": total,
                  "insurance_pay": ins, "self_pay": self_pay},
            headers=admin,
        )
        assert resp.status_code == 201, resp.text
        return resp.json()

    settle(data["org_t"], "local", 100, 70, 30)
    settle(data["org_c"], "remote", 200, 120, 80)
    settle(data["org_c"], "local", 40, 20, 20)
    data["fund_int"] = client.get("/api/insurance/fund-stats", headers=data["director"]).json()
    settle(data["org_t"], "local", 10.5, 0.5, 10)

    # 转诊证明：未接诊先拒签（409 走错误用例），接诊后签发并幂等复签
    referral = client.post(
        "/api/referrals",
        json={"patient_id": data["patient"]["id"], "from_org_id": data["org_t"]["id"],
              "to_org_id": data["org_c"]["id"], "direction": "up", "reason": "上转手术"},
        headers=admin,
    ).json()
    data["referral"] = referral
    data["cert_pending_resp"] = client.post(
        f"/api/insurance/referral-certs/{referral['id']}", headers=data["operator"]
    )
    assert client.patch(
        f"/api/referrals/{referral['id']}/status",
        json={"status": "accepted"},
        headers=data["doctor"],
    ).status_code == 200
    data["cert1"] = client.post(
        f"/api/insurance/referral-certs/{referral['id']}", headers=data["operator"]
    ).json()
    data["cert2"] = client.post(
        f"/api/insurance/referral-certs/{referral['id']}", headers=data["operator"]
    ).json()

    # 双通道：申报两单（带/不带 reason）→ 一单批准（带意见）一单驳回（无意见）
    resp = client.post(
        "/api/insurance/dual-channel",
        json={"patient_id": data["patient"]["id"], "drug_name": "利妥昔单抗(契约)",
              "reason": "淋巴瘤一线用药"},
        headers=data["operator"],
    )
    assert resp.status_code == 201, resp.text
    data["dc1"] = resp.json()
    data["dc2"] = client.post(
        "/api/insurance/dual-channel",
        json={"patient_id": data["patient"]["id"], "drug_name": "诺西那生钠(契约)"},
        headers=data["operator"],
    ).json()
    data["rev1"] = client.post(
        f"/api/insurance/dual-channel/{data['dc1']['id']}/review?approve=true&comment=符合双通道条件",
        headers=data["director"],
    ).json()
    data["rev2"] = client.post(
        f"/api/insurance/dual-channel/{data['dc2']['id']}/review?approve=false",
        headers=data["director"],
    ).json()
    return data


# ---------------------------------------------------------------- 基金监测


def test_基金监测空库分支_全部是浮点兜底(seed):
    body = seed["fund_empty"]
    assert list(body.keys()) == FUND_KEYS
    assert body == {
        "insurance_pay_total": 0.0,
        "local_ratio_pct": 0.0,
        "grassroots_ratio_pct": 0.0,
    }
    # coalesce(sum(...), 0.0) 的兜底字面量是 0.0 不是 0
    assert all(isinstance(body[k], float) for k in FUND_KEYS)


def test_基金监测整数金额_int与float之别(seed):
    body = seed["fund_int"]
    assert list(body.keys()) == FUND_KEYS
    assert body == {
        "insurance_pay_total": 210,
        "local_ratio_pct": round(90 * 100.0 / 210, 2),
        "grassroots_ratio_pct": round(70 * 100.0 / 210, 2),
    }
    # Money 之和的整数值读回 int（声明成 float 会把 210 变 210.0，即改字节）
    assert type(body["insurance_pay_total"]) is int
    assert isinstance(body["local_ratio_pct"], float)
    assert isinstance(body["grassroots_ratio_pct"], float)


def test_基金监测小数金额_同字段变float(client, seed):
    body = client.get("/api/insurance/fund-stats", headers=seed["director"]).json()
    assert body == {
        "insurance_pay_total": 210.5,
        "local_ratio_pct": round(90.5 * 100.0 / 210.5, 2),
        "grassroots_ratio_pct": round(70.5 * 100.0 / 210.5, 2),
    }
    assert isinstance(body["insurance_pay_total"], float)


# ---------------------------------------------------------------- 转诊证明


def test_转诊证明回执精确_幂等复签同号(seed):
    body = seed["cert1"]
    assert list(body.keys()) == CERT_KEYS
    assert body == {"cert_no": body["cert_no"], "referral_id": seed["referral"]["id"]}
    # 证明号 ZZ + 10 位十六进制大写（随机项，只钉形状）
    assert body["cert_no"].startswith("ZZ") and len(body["cert_no"]) == 12
    # 幂等：重复签发返回同一张证明，不换号
    assert seed["cert2"] == body


# ---------------------------------------------------------------- 双通道药品申报


def test_双通道申报与审核回执精确(seed):
    body = seed["dc1"]
    assert list(body.keys()) == DUAL_APPLY_KEYS
    assert body == {"id": body["id"], "status": "pending", "drug_name": "利妥昔单抗(契约)"}
    assert seed["dc2"] == {"id": seed["dc2"]["id"], "status": "pending",
                           "drug_name": "诺西那生钠(契约)"}

    rev1 = seed["rev1"]
    assert list(rev1.keys()) == DUAL_REVIEW_KEYS
    assert rev1 == {"id": seed["dc1"]["id"], "status": "approved"}
    assert seed["rev2"] == {"id": seed["dc2"]["id"], "status": "rejected"}


def test_双通道列表精确_键序与过滤(client, seed):
    rows = client.get("/api/insurance/dual-channel", headers=seed["operator"]).json()
    assert [list(r.keys()) for r in rows] == [DUAL_ROW_KEYS] * 2
    # id 倒序；驳回单无意见（空串不是 null），批准单带审核意见
    assert rows == [
        {
            "id": seed["dc2"]["id"],
            "patient_id": seed["patient"]["id"],
            "drug_name": "诺西那生钠(契约)",
            "reason": "",
            "status": "rejected",
            "review_comment": "",
        },
        {
            "id": seed["dc1"]["id"],
            "patient_id": seed["patient"]["id"],
            "drug_name": "利妥昔单抗(契约)",
            "reason": "淋巴瘤一线用药",
            "status": "approved",
            "review_comment": "符合双通道条件",
        },
    ]
    assert client.get(
        "/api/insurance/dual-channel?status=approved", headers=seed["operator"]
    ).json() == [rows[1]]
    assert client.get(
        "/api/insurance/dual-channel?status=pending", headers=seed["operator"]
    ).json() == []


# ---------------------------------------------------------------- 错误体


def test_各类错误体都只有detail(client, admin, seed):
    cases = [
        seed["cert_pending_resp"],  # 未接诊签发 409（种子里先行触发）
        client.post("/api/insurance/referral-certs/999999", headers=seed["operator"]),  # 404
        client.post("/api/insurance/dual-channel",
                    json={"patient_id": 999999, "drug_name": "无"},
                    headers=seed["operator"]),  # 404
        client.post("/api/insurance/dual-channel/999999/review?approve=true",
                    headers=seed["director"]),  # 404
        client.post(f"/api/insurance/dual-channel/{seed['dc1']['id']}/review?approve=false",
                    headers=seed["director"]),  # 已处理 409
        client.post("/api/insurance/settlements",
                    json={"patient_id": seed["patient"]["id"], "org_id": seed["org_t"]["id"],
                          "total_amount": 100, "insurance_pay": 70, "self_pay": 20},
                    headers=admin),  # 医保+自付≠总额 422
    ]
    assert [r.status_code for r in cases] == [409, 404, 404, 404, 409, 422]
    for r in cases:
        assert set(r.json()) == {"detail"}
