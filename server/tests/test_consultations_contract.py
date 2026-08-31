"""远程会诊 `/api/consultations` 两个待治理端点（计费/统计）的**特征化网 + 响应契约**。

套路同 test_maternal_contract.py / test_users_contract.py：先钉住**当前**响应的
完整 JSON（dict 相等）与键序 → 再加 `response_model` → 加完逐字节不变
（CLAUDE.md §11）。其余 8 个端点已治理（ConsultationOut 等），仅作种子。

本簇的建模判断（都以此处的精确断言为依据）：

- `Consultation.fee` 是 **Money 列**（Numeric asdecimal=False）：整数金额读回
  是 `int`（200 不得变 200.0），小数是 float → 计费回执与统计的
  `total_amount` 一律 `int | float`，两种取值各钉一遍且用 `type(x) is int`
  显式钉（dict 相等对 15==15.0 是盲的）。零分支 `round(sum([]), 2)` 是
  **int 0**，在造数前单独钉。
- `completion_rate_pct` / `rating.avg` 是真除法派生：有值必 float、空分支
  null → `float | None`（键恒在值可空，非条件键）。
- `by_status` 键是状态码、随数据变 → `dict[str, int]`，键序=按会诊 id 升序
  首遇的状态序，逐键钉死。
- 计费回执四键与统计六键不同形；统计的 rating/fee 是嵌套子形状，各自建模。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app

STATS_KEYS = ["total", "by_status", "completion_rate_pct", "rating", "fee", "caliber"]
CALIBER = "评分均值只算已评价的（rating>0），未评价单列；fee=0 与未计费是两回事，后者看 fee_settled"


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def test_统计零分支精确_空表金额是int0(client, admin):
    """放在最前：空表的 null 比率与 round(sum([]),2)=0（int）才钉得住。"""
    body = client.get("/api/consultations/stats", headers=admin).json()
    assert list(body.keys()) == STATS_KEYS
    assert body == {
        "total": 0,
        "by_status": {},
        "completion_rate_pct": None,
        "rating": {"rated_count": 0, "unrated_count": 0, "avg": None},
        "fee": {"settled_count": 0, "unsettled_count": 0, "total_amount": 0},
        "caliber": CALIBER,
    }
    assert type(body["fee"]["total_amount"]) is int  # 空分支是 int 0，不是 0.0


@pytest.fixture(scope="module")
def flow(client, admin):
    """c1 完成+评价+整数计费；c2 完成+小数计费；c3 挂在已申请。"""
    org_from = client.post(
        "/api/organizations",
        json={"name": "会诊契约卫生院", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()
    org_to = client.post(
        "/api/organizations",
        json={"name": "会诊契约总院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    patient = client.post(
        "/api/patients",
        json={"name": "契约会诊患者", "id_card": "330281197807078857", "gender": "男",
              "birth_date": "1978-07-07"},
        headers=admin,
    ).json()

    def consult(question):
        return client.post(
            "/api/consultations",
            json={"patient_id": patient["id"], "from_org_id": org_from["id"],
                  "to_org_id": org_to["id"], "question": question},
            headers=admin,
        ).json()

    c1 = consult("胸部影像会诊")
    client.post(f"/api/consultations/{c1['id']}/accept", json={"expert_name": "李主任"}, headers=admin)
    client.post(f"/api/consultations/{c1['id']}/complete", json={"opinion": "考虑良性结节"}, headers=admin)
    client.post(f"/api/consultations/{c1['id']}/rate", json={"rating": 5}, headers=admin)
    return {"c1": c1, "consult": consult}


def test_计费回执精确_整数金额是int(client, admin, flow):
    body = client.post(
        f"/api/consultations/{flow['c1']['id']}/fee",
        json={"fee": 200, "fee_note": "县级专家会诊费"},
        headers=admin,
    ).json()
    assert list(body.keys()) == ["id", "fee", "fee_settled", "fee_note"]
    assert body == {
        "id": flow["c1"]["id"], "fee": 200, "fee_settled": True, "fee_note": "县级专家会诊费"
    }
    assert type(body["fee"]) is int  # Money 列整数金额：200 不得变 200.0


def test_统计中段精确_整数总额是int(client, admin, flow):
    body = client.get("/api/consultations/stats", headers=admin).json()
    assert body == {
        "total": 1,
        "by_status": {"completed": 1},
        "completion_rate_pct": 100.0,
        "rating": {"rated_count": 1, "unrated_count": 0, "avg": 5.0},
        "fee": {"settled_count": 1, "unsettled_count": 0, "total_amount": 200},
        "caliber": CALIBER,
    }
    assert type(body["fee"]["total_amount"]) is int
    assert isinstance(body["completion_rate_pct"], float)
    assert isinstance(body["rating"]["avg"], float)


@pytest.fixture(scope="module")
def more_flow(client, admin, flow):
    c2 = flow["consult"]("病理切片会诊")
    client.post(f"/api/consultations/{c2['id']}/accept", json={"expert_name": "赵主任"}, headers=admin)
    client.post(f"/api/consultations/{c2['id']}/complete", json={"opinion": "建议随访复查"}, headers=admin)
    fee2 = client.post(f"/api/consultations/{c2['id']}/fee", json={"fee": 88.5}, headers=admin)
    assert fee2.status_code == 200, fee2.text
    c3 = flow["consult"]("待受理的会诊")
    return {"c2": c2, "fee2": fee2.json(), "c3": c3}


def test_计费回执精确_小数金额是float(more_flow):
    body = more_flow["fee2"]
    assert body == {"id": more_flow["c2"]["id"], "fee": 88.5, "fee_settled": True, "fee_note": ""}
    assert type(body["fee"]) is float


def test_计费_未完成409(client, admin, more_flow):
    resp = client.post(
        f"/api/consultations/{more_flow['c3']['id']}/fee", json={"fee": 10}, headers=admin
    )
    assert resp.status_code == 409
    assert resp.json() == {"detail": "仅已完成的会诊可计费"}


def test_统计精确形状与键序(client, admin, flow, more_flow):
    body = client.get("/api/consultations/stats", headers=admin).json()
    assert list(body.keys()) == STATS_KEYS
    assert list(body["by_status"].keys()) == ["completed", "applied"]  # 按会诊 id 升序首遇
    assert body == {
        "total": 3,
        "by_status": {"completed": 2, "applied": 1},
        "completion_rate_pct": 66.67,
        # c2 已完成未评价：单列到 unrated，不并进均值也不当 0 分
        "rating": {"rated_count": 1, "unrated_count": 1, "avg": 5.0},
        # 200(int) + 88.5(float) → 288.5：混入小数后整体是 float
        "fee": {"settled_count": 2, "unsettled_count": 0, "total_amount": 288.5},
        "caliber": CALIBER,
    }
    assert type(body["fee"]["total_amount"]) is float
