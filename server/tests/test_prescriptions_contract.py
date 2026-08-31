"""集中审方 `/api/prescriptions` 平台侧 7 个未治理端点的**特征化网 + 响应契约**。

套路同 `test_billing_contract.py` / `test_inpatient_contract.py`：先钉住**当前**
响应的完整 JSON（dict 相等）与键序 → 再加 `response_model` → 加完逐字节不变
（CLAUDE.md §7/§11）。已治理的 5 个端点（rules 建/列、处方建/列/审）不在此列。

本簇的建模判断（都以此处的精确断言为依据）：

- **`daily_dose` / `max_daily_dose` 是 Float 列**（`prescription_items.daily_dose`、
  `drug_rules.max_daily_dose`）：整数入参 4 落库读回就是 4.0，声明 `float`
  才是原样——与 Money 列相反，这里写 int|float 才是错的。
  无规则时 `max_daily_dose` 为 null——键恒在值可空 → `float | None`，
  不是条件键，不用 exclude_unset。
- **两个占比恒 float**：`rule_coverage_pct` / `reasonable_rate_pct` 的两条产地
  （`round(x*100.0/n, 2)` 真除法与空分母兜底字面量 `0.0`）全是浮点——
  种子先在零点评时取一次 0.0，再造出 50.0，两条产地各钉一遍。
- 停用与恢复回执同形（drug_code+active）共用一个模型；
  点评回执（3 键）与点评清单行（6 键，`at` 为 ISO 串）分开建模。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app

IMPORT_KEYS = ["imported", "updated"]
RULE_ACTIVE_KEYS = ["drug_code", "active"]
REVIEW_POINTS_KEYS = [
    "prescription_id", "diagnosis_name", "status", "system_review_comment",
    "items", "rule_coverage_pct",
]
REVIEW_POINT_ITEM_KEYS = [
    "drug_code", "drug_name", "daily_dose", "max_daily_dose", "dose_unit",
    "dose_exceeded", "review_points", "renal_hepatic_note", "no_rule",
]
COMMENT_CREATED_KEYS = ["id", "prescription_id", "grade"]
COMMENT_KEYS = ["id", "prescription_id", "grade", "issues", "comment", "at"]
COMMENT_STATS_KEYS = ["commented", "unreasonable", "reasonable_rate_pct"]


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

    规则：RXCT-A 直建（限量 3g，带点评要点与肝肾提示）→ import 改它并新建
    RXCT-B → 停用 RXCT-B → 恢复。处方：RX1 双药（RXCT-A 超量 + 无规则药，
    覆盖率 50.0）、RX2 单药有规则（覆盖率 100.0）。点评：零点评先取一次
    stats（0.0 兜底分支），再对 RX1 记不合理、RX2 记合理。
    """
    data: dict = {}
    org = client.post(
        "/api/organizations",
        json={"name": "契约审方医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    data["org"] = org
    for username, role in [("rxct_doc", "doctor"), ("rxct_pha", "pharmacist")]:
        client.post(
            "/api/users",
            json={"username": username, "password": "pass123456", "role": role, "org_id": org["id"]},
            headers=admin,
        )
        data[role] = login(client, username, "pass123456")
    data["patient"] = client.post(
        "/api/patients",
        json={"name": "契约审方患者", "id_card": "330881199001017901"},
        headers=admin,
    ).json()

    # 零点评时的统计：空分母兜底字面量 0.0 这条产地要先取到手
    data["stats_zero"] = client.get("/api/prescriptions/comment-stats", headers=admin).json()

    assert client.post(
        "/api/prescriptions/rules",
        json={"drug_code": "RXCT-A", "max_daily_dose": 3, "dose_unit": "g",
              "review_points": "核对疗程与联用", "renal_hepatic_note": "肾功能不全减量"},
        headers=admin,
    ).status_code == 201
    resp = client.post(
        "/api/prescriptions/rules/import",
        json=[
            {"drug_code": "RXCT-B", "max_daily_dose": 8, "dose_unit": "mg"},  # 新建
            {"drug_code": "RXCT-A", "max_daily_dose": 3, "dose_unit": "g",   # 已有→更新
             "review_points": "核对疗程与联用", "renal_hepatic_note": "肾功能不全减量"},
        ],
        headers=admin,
    )
    assert resp.status_code == 200, resp.text
    data["import"] = resp.json()
    data["deactivated"] = client.delete("/api/prescriptions/rules/RXCT-B", headers=admin).json()
    data["reactivated"] = client.post(
        "/api/prescriptions/rules/RXCT-B/reactivate", headers=admin
    ).json()

    def prescribe(items, diagnosis):
        resp = client.post(
            "/api/prescriptions",
            json={"patient_id": data["patient"]["id"], "org_id": org["id"],
                  "diagnosis_name": diagnosis, "items": items},
            headers=data["doctor"],
        )
        assert resp.status_code == 201, resp.text
        return resp.json()

    data["rx1"] = prescribe(
        [{"drug_code": "RXCT-A", "drug_name": "契约头孢", "daily_dose": 4, "days": 2},
         {"drug_code": "RXCT-X", "drug_name": "契约无规则药", "daily_dose": 1.5, "days": 1}],
        "呼吸道感染",
    )
    data["rx2"] = prescribe(
        [{"drug_code": "RXCT-B", "drug_name": "契约缬沙坦", "daily_dose": 8, "days": 7}],
        "高血压",
    )
    # 不点评的第三方：错误体用例要一个还没被点评的处方去踩 422 分支
    data["rx3"] = prescribe(
        [{"drug_code": "RXCT-X", "drug_name": "契约无规则药", "daily_dose": 1, "days": 1}],
        "随访复诊",
    )

    resp = client.post(
        f"/api/prescriptions/{data['rx1']['id']}/comment-review",
        json={"grade": "unreasonable", "issues": "用法用量不适宜", "comment": "日剂量超上限"},
        headers=data["pharmacist"],
    )
    assert resp.status_code == 201, resp.text
    data["comment1"] = resp.json()
    data["comment2"] = client.post(
        f"/api/prescriptions/{data['rx2']['id']}/comment-review",
        json={"grade": "reasonable"},
        headers=data["pharmacist"],
    ).json()
    return data


# ---------------------------------------------------------------- 规则维护


def test_规则批量导入回执精确_键序(seed):
    body = seed["import"]
    assert list(body.keys()) == IMPORT_KEYS
    assert body == {"imported": 1, "updated": 1}
    assert type(body["imported"]) is int


def test_规则停用与恢复回执同形(seed):
    assert list(seed["deactivated"].keys()) == RULE_ACTIVE_KEYS
    assert seed["deactivated"] == {"drug_code": "RXCT-B", "active": False}
    assert seed["reactivated"] == {"drug_code": "RXCT-B", "active": True}
    # bool 不得被声明成 int：false 变 0 就是改字节
    assert seed["deactivated"]["active"] is False


# ---------------------------------------------------------------- 点评要点


def test_点评要点精确_Float列与可空上限(client, admin, seed):
    body = client.get(
        f"/api/prescriptions/{seed['rx1']['id']}/review-points", headers=admin
    ).json()
    assert list(body.keys()) == REVIEW_POINTS_KEYS
    assert [list(i.keys()) for i in body["items"]] == [REVIEW_POINT_ITEM_KEYS] * 2
    assert body == {
        "prescription_id": seed["rx1"]["id"],
        "diagnosis_name": "呼吸道感染",
        "status": "pending_review",
        "system_review_comment": "契约头孢 日剂量 4.0g 超过上限 3.0g",
        "items": [
            {
                "drug_code": "RXCT-A",
                "drug_name": "契约头孢",
                "daily_dose": 4.0,
                "max_daily_dose": 3.0,
                "dose_unit": "g",
                "dose_exceeded": True,
                "review_points": "核对疗程与联用",
                "renal_hepatic_note": "肾功能不全减量",
                "no_rule": False,
            },
            {
                "drug_code": "RXCT-X",
                "drug_name": "契约无规则药",
                "daily_dose": 1.5,
                "max_daily_dose": None,  # 无规则：键在值空，不是键消失
                "dose_unit": "",
                "dose_exceeded": False,
                "review_points": "",
                "renal_hepatic_note": "",
                "no_rule": True,
            },
        ],
        "rule_coverage_pct": 50.0,
    }
    # Float 列：整数入参 4 落库读回 4.0（与 Money 列相反）
    assert isinstance(body["items"][0]["daily_dose"], float)
    assert isinstance(body["items"][0]["max_daily_dose"], float)
    assert isinstance(body["rule_coverage_pct"], float)


def test_点评要点_全覆盖时coverage是100点0(client, admin, seed):
    body = client.get(
        f"/api/prescriptions/{seed['rx2']['id']}/review-points", headers=admin
    ).json()
    assert body["rule_coverage_pct"] == 100.0
    assert isinstance(body["rule_coverage_pct"], float)
    assert body["items"][0]["dose_exceeded"] is False  # 8.0 未超 8.0
    assert body["status"] == "auto_passed" and body["system_review_comment"] == ""


# ---------------------------------------------------------------- 处方点评


def test_点评回执精确_三键封口(seed):
    body = seed["comment1"]
    assert list(body.keys()) == COMMENT_CREATED_KEYS
    assert body == {"id": body["id"], "prescription_id": seed["rx1"]["id"],
                    "grade": "unreasonable"}
    assert seed["comment2"] == {"id": seed["comment2"]["id"],
                                "prescription_id": seed["rx2"]["id"], "grade": "reasonable"}


def test_点评清单精确_过滤(client, admin, seed):
    rows = client.get("/api/prescriptions/comment-reviews", headers=admin).json()
    assert [list(r.keys()) for r in rows] == [COMMENT_KEYS] * 2  # id 倒序
    assert rows == [
        {"id": seed["comment2"]["id"], "prescription_id": seed["rx2"]["id"],
         "grade": "reasonable", "issues": "", "comment": "", "at": rows[0]["at"]},
        {"id": seed["comment1"]["id"], "prescription_id": seed["rx1"]["id"],
         "grade": "unreasonable", "issues": "用法用量不适宜", "comment": "日剂量超上限",
         "at": rows[1]["at"]},
    ]
    assert isinstance(rows[0]["at"], str)
    assert client.get(
        "/api/prescriptions/comment-reviews?grade=unreasonable", headers=admin
    ).json() == [rows[1]]


def test_点评统计精确_两条产地都是float(client, admin, seed):
    zero = seed["stats_zero"]
    assert list(zero.keys()) == COMMENT_STATS_KEYS
    assert zero == {"commented": 0, "unreasonable": 0, "reasonable_rate_pct": 0.0}
    # 空分母兜底是字面量 0.0——声明 int|float 不算错字节，但 float 才是两条产地的公共真相
    assert isinstance(zero["reasonable_rate_pct"], float)
    body = client.get("/api/prescriptions/comment-stats", headers=admin).json()
    assert body == {"commented": 2, "unreasonable": 1, "reasonable_rate_pct": 50.0}
    assert type(body["commented"]) is int
    assert isinstance(body["reasonable_rate_pct"], float)


# ---------------------------------------------------------------- 错误体


def test_各类错误体都只有detail(client, admin, seed):
    cases = [
        client.get("/api/prescriptions/999999/review-points", headers=admin),  # 404
        client.delete("/api/prescriptions/rules/NO-SUCH", headers=admin),  # 404
        client.post("/api/prescriptions/rules/NO-SUCH/reactivate", headers=admin),  # 404
        client.post("/api/prescriptions/999999/comment-review",
                    json={"grade": "reasonable"}, headers=seed["pharmacist"]),  # 404
        client.post(f"/api/prescriptions/{seed['rx1']['id']}/comment-review",
                    json={"grade": "reasonable"}, headers=seed["pharmacist"]),  # 已点评 409
        client.post(f"/api/prescriptions/{seed['rx3']['id']}/comment-review",
                    json={"grade": "unreasonable"}, headers=seed["pharmacist"]),  # 未注明问题 422
    ]
    assert [r.status_code for r in cases] == [404, 404, 404, 404, 409, 422]
    for r in cases:
        assert set(r.json()) == {"detail"}
    # 停用已停用的规则：先停一次再停第二次 → 409
    client.delete("/api/prescriptions/rules/RXCT-B", headers=admin)
    twice = client.delete("/api/prescriptions/rules/RXCT-B", headers=admin)
    assert twice.status_code == 409 and set(twice.json()) == {"detail"}
    client.post("/api/prescriptions/rules/RXCT-B/reactivate", headers=admin)  # 复位
