"""妇幼保健 `/api/maternal` 十二个待治理端点的**特征化网 + 响应契约**。

套路同 test_quality_contract.py / test_education_contract.py：先补网钉住**当前**
响应的完整 JSON（dict 相等）与键序 → 再加 `response_model` → 加完逐字节不变
（CLAUDE.md §11）。（records/children/women-health 六个端点此前已治理，
不在本网范围，仅作种子。）

本簇的建模判断（都以此处的精确断言为依据）：

- 本簇**没有 Money/Float 列出参**，数值全是 Integer/Boolean；唯一的比率
  `high_risk_detect_rate_pct` 恒 float（`round(x*100.0/n, 2)` 与兜底字面量 `0.0`
  两条分支都是浮点，零分支在建任何筛查前单独钉住）。
- `gest_week` 是**值可空**而非条件键：键恒在，未填时值为 null——声明
  `int | None` 即可，无需 exclude_unset。本簇没有条件键。
- 产前筛查回执与列表行同形（`_screening_out` 唯一产地，10 键含
  `screen_type_name` 派生名与 `flagged_high_risk` 联动标记），共用一个模型。
- `screening-stats` 的 `by_type` 值是 {count, name} 两键子形状、`by_result` 是
  `dict[str, int]`——键为筛查类型/结论代码，随数据变，宽 dict + 精确断言钉住。
- 访视回执（id/record_id/high_risk/status）回显**档案**联动状态；儿童访视回执
  只有 id/child_id 两键——两个模型，不许互相注入。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app

VISIT_RECEIPT_KEYS = ["id", "record_id", "high_risk", "status"]
DELIVERY_DETAIL_KEYS = [
    "id", "record_id", "org_id", "delivery_date", "delivery_mode", "newborn_count", "outcome",
]
PRENATAL_SCREENING_KEYS = [
    "id", "record_id", "screen_type", "screen_type_name", "screen_date",
    "gest_week", "result", "indicator", "conclusion", "flagged_high_risk",
]
NEWBORN_SCREENING_ROW_KEYS = ["id", "item", "result", "screen_date", "note"]


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


def test_产前筛查统计_零分支精确(client, admin):
    """放在最前：此刻还没有任何产前筛查记录，零分支的 0.0 才钉得住。"""
    resp = client.get("/api/maternal/screening-stats", headers=admin)
    assert list(resp.json().keys()) == ["total", "by_type", "by_result", "high_risk_detect_rate_pct"]
    assert resp.json() == {"total": 0, "by_type": {}, "by_result": {}, "high_risk_detect_rate_pct": 0.0}
    assert isinstance(resp.json()["high_risk_detect_rate_pct"], float)


@pytest.fixture(scope="module")
def base(client, admin):
    org = client.post(
        "/api/organizations",
        json={"name": "妇幼契约医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    patient = client.post(
        "/api/patients",
        json={"name": "契约孕妇", "id_card": "330281199505016024", "gender": "女", "birth_date": "1995-05-01"},
        headers=admin,
    ).json()
    record = client.post(
        "/api/maternal/records",
        json={"patient_id": patient["id"], "lmp": "2026-01-10", "edc": "2026-10-17",
              "gravidity": 2, "parity": 1},
        headers=admin,
    ).json()
    return {"org": org, "patient": patient, "record": record}


# ---------------------------------------------------------------- 产检/分娩/结案


@pytest.fixture(scope="module")
def record_flow(client, admin, base):
    """一本册子走完 产检（正常/高血压自动高危）→ 分娩 → 产后访视 → 结案。"""
    rid = base["record"]["id"]
    v1 = client.post(
        f"/api/maternal/records/{rid}/visits",
        json={"visit_type": "prenatal", "gest_week": 12, "bp": "118/76",
              "note": "一切正常", "visit_date": "2026-04-01"},
        headers=admin,
    )
    assert v1.status_code == 201, v1.text
    v2 = client.post(
        f"/api/maternal/records/{rid}/visits",
        json={"visit_type": "prenatal", "gest_week": 20, "bp": "150/95", "visit_date": "2026-05-29"},
        headers=admin,
    ).json()
    delivery = client.post(
        f"/api/maternal/records/{rid}/delivery",
        json={"org_id": base["org"]["id"], "delivery_date": "2026-10-10",
              "delivery_mode": "cesarean", "outcome": "母子平安"},
        headers=admin,
    )
    assert delivery.status_code == 201, delivery.text
    v3 = client.post(
        f"/api/maternal/records/{rid}/visits",
        json={"visit_type": "postpartum", "note": "产后42天访视", "visit_date": "2026-11-21"},
        headers=admin,
    ).json()
    closed = client.post(f"/api/maternal/records/{rid}/close", headers=admin)
    assert closed.status_code == 200, closed.text
    return {"v1": v1.json(), "v2": v2, "delivery": delivery.json(),
            "v3": v3, "closed": closed.json()}


def test_产检回执精确_高血压自动高危(base, record_flow):
    rid = base["record"]["id"]
    body = record_flow["v1"]
    assert list(body.keys()) == VISIT_RECEIPT_KEYS
    assert body == {"id": body["id"], "record_id": rid, "high_risk": False, "status": "registered"}
    # 收缩压≥140：档案联动标记高危，回执如实回显
    assert record_flow["v2"] == {
        "id": record_flow["v2"]["id"], "record_id": rid, "high_risk": True, "status": "registered"
    }
    # 分娩后的产后访视：status 已是 delivered
    assert record_flow["v3"] == {
        "id": record_flow["v3"]["id"], "record_id": rid, "high_risk": True, "status": "delivered"
    }


def test_分娩回执与查询精确(client, admin, base, record_flow):
    rid = base["record"]["id"]
    body = record_flow["delivery"]
    assert list(body.keys()) == ["id", "record_id", "delivery_mode", "status"]
    assert body == {"id": body["id"], "record_id": rid, "delivery_mode": "cesarean", "status": "delivered"}
    detail = client.get(f"/api/maternal/records/{rid}/delivery", headers=admin).json()
    assert list(detail.keys()) == DELIVERY_DETAIL_KEYS
    assert detail == {
        "id": body["id"],
        "record_id": rid,
        "org_id": base["org"]["id"],
        "delivery_date": "2026-10-10",
        "delivery_mode": "cesarean",
        "newborn_count": 1,
        "outcome": "母子平安",
    }


def test_结案回执精确(base, record_flow):
    assert list(record_flow["closed"].keys()) == ["id", "status"]
    assert record_flow["closed"] == {"id": base["record"]["id"], "status": "closed"}


# ---------------------------------------------------------------- 儿童保健与新筛


@pytest.fixture(scope="module")
def children(client, admin, base):
    """ch1 新筛异常自动纳入高危后人工解除；ch2 人工标记——高危清单只剩 ch2。"""
    ch1 = client.post(
        "/api/maternal/children",
        json={"name": "契约宝宝", "gender": "男", "birth_date": "2026-10-10",
              "guardian_patient_id": base["patient"]["id"]},
        headers=admin,
    ).json()
    ch2 = client.post(
        "/api/maternal/children", json={"name": "契约二宝", "birth_date": "2026-06-01"}, headers=admin
    ).json()
    visit = client.post(
        f"/api/maternal/children/{ch1['id']}/visits",
        json={"visit_type": "newborn", "height_cm": 50.5, "weight_kg": 3.4,
              "note": "新生儿访视", "visit_date": "2026-10-20"},
        headers=admin,
    )
    assert visit.status_code == 201, visit.text
    s1 = client.post(
        f"/api/maternal/children/{ch1['id']}/screenings",
        json={"item": "metabolic", "result": "normal", "screen_date": "2026-10-12"},
        headers=admin,
    )
    assert s1.status_code == 201, s1.text
    s2 = client.post(
        f"/api/maternal/children/{ch1['id']}/screenings",
        json={"item": "hearing", "result": "abnormal", "screen_date": "2026-10-13", "note": "左耳未通过"},
        headers=admin,
    ).json()
    hr_set = client.post(
        f"/api/maternal/children/{ch2['id']}/high-risk",
        json={"high_risk": True, "risk_note": "早产低体重"},
        headers=admin,
    )
    assert hr_set.status_code == 200, hr_set.text
    hr_unset = client.post(
        f"/api/maternal/children/{ch1['id']}/high-risk", json={"high_risk": False}, headers=admin
    ).json()
    return {"ch1": ch1, "ch2": ch2, "visit": visit.json(), "s1": s1.json(), "s2": s2,
            "hr_set": hr_set.json(), "hr_unset": hr_unset}


def test_儿童访视回执精确_只有两键(children):
    body = children["visit"]
    assert list(body.keys()) == ["id", "child_id"]
    assert body == {"id": body["id"], "child_id": children["ch1"]["id"]}


def test_新筛回执精确_异常自动纳入高危(children):
    body = children["s1"]
    assert list(body.keys()) == ["id", "child_id", "item", "result", "child_high_risk"]
    assert body == {
        "id": body["id"],
        "child_id": children["ch1"]["id"],
        "item": "metabolic",
        "result": "normal",
        "child_high_risk": False,
    }
    assert children["s2"] == {
        "id": children["s2"]["id"],
        "child_id": children["ch1"]["id"],
        "item": "hearing",
        "result": "abnormal",
        "child_high_risk": True,
    }


def test_新筛清单精确(client, admin, children):
    rows = client.get(
        f"/api/maternal/children/{children['ch1']['id']}/screenings", headers=admin
    ).json()
    assert [list(r.keys()) for r in rows] == [NEWBORN_SCREENING_ROW_KEYS] * 2
    assert rows == [
        {"id": children["s1"]["id"], "item": "metabolic", "result": "normal",
         "screen_date": "2026-10-12", "note": ""},
        {"id": children["s2"]["id"], "item": "hearing", "result": "abnormal",
         "screen_date": "2026-10-13", "note": "左耳未通过"},
    ]  # id 正序


def test_高危儿标记回执与清单精确(client, admin, children):
    assert list(children["hr_set"].keys()) == ["id", "high_risk", "risk_note"]
    assert children["hr_set"] == {
        "id": children["ch2"]["id"], "high_risk": True, "risk_note": "早产低体重"
    }
    # 解除：risk_note 联动清空
    assert children["hr_unset"] == {"id": children["ch1"]["id"], "high_risk": False, "risk_note": ""}
    rows = client.get("/api/maternal/children/high-risk", headers=admin).json()
    assert [list(r.keys()) for r in rows] == [["id", "name", "birth_date", "risk_note"]]
    assert rows == [{
        "id": children["ch2"]["id"],
        "name": "契约二宝",
        "birth_date": "2026-06-01",
        "risk_note": "早产低体重",
    }]


# ---------------------------------------------------------------- 产前筛查与诊断


@pytest.fixture(scope="module")
def prenatal(client, admin, base, record_flow):
    """依赖 record_flow：统计零分支已在建档前单独钉过，这里开始造数。"""
    low = client.post(
        "/api/maternal/screenings",
        json={"record_id": base["record"]["id"], "screen_type": "down", "screen_date": "2026-04-15",
              "gest_week": 16, "result": "low_risk", "indicator": "1/1200"},
        headers=admin,
    )
    assert low.status_code == 201, low.text
    high = client.post(
        "/api/maternal/screenings",
        json={"record_id": base["record"]["id"], "screen_type": "nipt", "screen_date": "2026-05-06",
              "result": "high_risk", "conclusion": "建议产前诊断"},
        headers=admin,
    ).json()
    return {"low": low.json(), "high": high}


def test_产前筛查回执精确形状与键序(base, prenatal):
    body = prenatal["low"]
    assert list(body.keys()) == PRENATAL_SCREENING_KEYS
    assert body == {
        "id": body["id"],
        "record_id": base["record"]["id"],
        "screen_type": "down",
        "screen_type_name": "唐氏血清学筛查",
        "screen_date": "2026-04-15",
        "gest_week": 16,
        "result": "low_risk",
        "indicator": "1/1200",
        "conclusion": "",
        "flagged_high_risk": False,
    }
    # gest_week 是**值可空**的恒在键（未填为 null，不是键消失）；高风险联动标记
    assert prenatal["high"] == {
        "id": prenatal["high"]["id"],
        "record_id": base["record"]["id"],
        "screen_type": "nipt",
        "screen_type_name": "无创产前基因检测",
        "screen_date": "2026-05-06",
        "gest_week": None,
        "result": "high_risk",
        "indicator": "",
        "conclusion": "建议产前诊断",
        "flagged_high_risk": True,
    }


def test_产前筛查列表与回执同形(client, admin, prenatal):
    rows = client.get("/api/maternal/screenings", headers=admin).json()
    assert rows == [prenatal["high"], prenatal["low"]]  # id 倒序
    assert client.get("/api/maternal/screenings?result=high_risk", headers=admin).json() == [
        prenatal["high"]
    ]
    assert client.get(
        f"/api/maternal/screenings?record_id={prenatal['low']['record_id']}", headers=admin
    ).json() == [prenatal["high"], prenatal["low"]]


def test_产前筛查统计精确(client, admin, prenatal):
    resp = client.get("/api/maternal/screening-stats", headers=admin)
    body = resp.json()
    assert list(body.keys()) == ["total", "by_type", "by_result", "high_risk_detect_rate_pct"]
    assert body == {
        "total": 2,
        "by_type": {
            "down": {"count": 1, "name": "唐氏血清学筛查"},
            "nipt": {"count": 1, "name": "无创产前基因检测"},
        },
        "by_result": {"high_risk": 1, "low_risk": 1},
        "high_risk_detect_rate_pct": 50.0,
    }
    assert isinstance(body["high_risk_detect_rate_pct"], float)
