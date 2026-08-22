"""住院押金（工程包 B1）：预交 → 计费 → 结算自动冲抵 → 出院 → 退余额 的闭环。

余额是流水现算的（admissions 冻结，不能加余额列）；退费/冲抵不得超余额
由 INSERT..SELECT 单条 SQL 原子判定——这里盯住业务面：超退 422、
冲抵口径（min(余额, 自付)）、退完为止。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture(scope="module")
def admin(client):
    token = client.post(
        "/api/auth/login", json={"username": "admin", "password": "admin123"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post(
        "/api/organizations",
        json={"name": "押金县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()


@pytest.fixture(scope="module")
def ward(client, admin, org):
    return client.post(
        "/api/inpatient/wards", json={"org_id": org["id"], "name": "押金一病区"}, headers=admin
    ).json()


_seq = [0]


def _admit(client, admin, ward):
    """新造一个患者 + 床位并办入院，返回 admission JSON。"""
    k = _seq[0]
    _seq[0] += 1
    patient = client.post(
        "/api/patients",
        json={"name": f"押金患者{k}", "id_card": f"3300001990010144{k:02d}"},
        headers=admin,
    ).json()
    bed = client.post(
        "/api/inpatient/beds", json={"ward_id": ward["id"], "bed_no": f"D-{k:02d}"}, headers=admin
    ).json()
    resp = client.post(
        "/api/inpatient/admissions",
        json={"patient_id": patient["id"], "ward_id": ward["id"], "bed_id": bed["id"],
              "doctor_name": "王医师", "diagnosis_name": "肺炎"},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _charge_item(client, admin, code, price):
    resp = client.post(
        "/api/billing/charge-items",
        json={"code": code, "name": f"项目{code}", "category": "treatment", "price": price},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text


def _bill(client, admin, admission, code, quantity=1):
    resp = client.post(
        "/api/billing/details",
        json={"patient_id": admission["patient_id"], "admission_id": admission["id"],
              "item_code": code, "quantity": quantity},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text


def _balance(client, admin, admission_id):
    return client.get(
        "/api/billing/deposits/balance", params={"admission_id": admission_id}, headers=admin
    ).json()


def test_预交与退费_退不得超余额(client, admin, ward):
    admission = _admit(client, admin, ward)
    dep = client.post(
        "/api/billing/deposits",
        json={"admission_id": admission["id"], "amount": 500, "method": "cash"},
        headers=admin,
    )
    assert dep.status_code == 201, dep.text
    assert dep.json()["balance"] == 500.0
    # 超余额退费：422，且一分不动
    over = client.post(
        "/api/billing/deposits/refund",
        json={"admission_id": admission["id"], "amount": 600},
        headers=admin,
    )
    assert over.status_code == 422
    assert "超出押金余额" in over.json()["detail"]
    assert _balance(client, admin, admission["id"])["balance"] == 500.0
    # 正常退 100
    ok = client.post(
        "/api/billing/deposits/refund",
        json={"admission_id": admission["id"], "amount": 100},
        headers=admin,
    )
    assert ok.status_code == 201
    assert ok.json()["deposit_type"] == "refund" and ok.json()["balance"] == 400.0
    # 流水只增不改：两笔都在
    rows = client.get(
        "/api/billing/deposits", params={"admission_id": admission["id"]}, headers=admin
    ).json()
    assert [r["deposit_type"] for r in rows] == ["refund", "prepay"]


def test_结算自动冲抵_余额够抵全额(client, admin, ward):
    """余额 400 ≥ 自付 300：冲抵 300，患者补 0，余 100 可退——闭环到出院退款。"""
    admission = _admit(client, admin, ward)
    client.post(
        "/api/billing/deposits",
        json={"admission_id": admission["id"], "amount": 400},
        headers=admin,
    )
    _charge_item(client, admin, "DEP300", 300)
    _bill(client, admin, admission, "DEP300")
    settle = client.post(
        "/api/billing/settlements",
        json={"bill_type": "inpatient", "admission_id": admission["id"], "insurance_pay": 0},
        headers=admin,
    )
    assert settle.status_code == 201, settle.text
    body = settle.json()
    assert body["self_pay"] == 300.0
    assert body["deposit_offset"] == 300.0, "冲抵额应等于个人自付"
    assert body["payable_after_offset"] == 0.0
    assert body["deposit_balance"] == 100.0
    bal = _balance(client, admin, admission["id"])
    assert bal["offset"] == 300.0 and bal["balance"] == 100.0
    # 出院后退掉剩余押金，闭环结束；再退一分都不行
    client.post(
        f"/api/inpatient/admissions/{admission['id']}/case-summary",
        json={"discharge_diagnosis": "肺炎痊愈", "total_cost": 300, "drug_cost": 0},
        headers=admin,
    )
    assert client.post(
        f"/api/inpatient/admissions/{admission['id']}/discharge", headers=admin
    ).status_code == 200
    assert client.post(
        "/api/billing/deposits/refund",
        json={"admission_id": admission["id"], "amount": 100},
        headers=admin,
    ).status_code == 201
    assert _balance(client, admin, admission["id"])["balance"] == 0.0
    assert client.post(
        "/api/billing/deposits/refund",
        json={"admission_id": admission["id"], "amount": 0.01},
        headers=admin,
    ).status_code == 422
    # 出院后不可再预交
    assert client.post(
        "/api/billing/deposits",
        json={"admission_id": admission["id"], "amount": 100},
        headers=admin,
    ).status_code == 409


def test_结算自动冲抵_余额不够抵差额补缴(client, admin, ward):
    """余额 100 < 自付 250：全部押金冲抵，差额口径 payable_after_offset=150。"""
    admission = _admit(client, admin, ward)
    client.post(
        "/api/billing/deposits",
        json={"admission_id": admission["id"], "amount": 100},
        headers=admin,
    )
    _charge_item(client, admin, "DEP250", 250)
    _bill(client, admin, admission, "DEP250")
    body = client.post(
        "/api/billing/settlements",
        json={"bill_type": "inpatient", "admission_id": admission["id"], "insurance_pay": 0},
        headers=admin,
    ).json()
    assert body["deposit_offset"] == 100.0, "余额不够时应把押金全部抵掉"
    assert body["payable_after_offset"] == 150.0, "差额=自付-冲抵，由患者补缴"
    assert body["deposit_balance"] == 0.0


def test_无押金结算不冲抵_响应口径仍在(client, admin, ward):
    admission = _admit(client, admin, ward)
    _charge_item(client, admin, "DEP80", 80)
    _bill(client, admin, admission, "DEP80")
    body = client.post(
        "/api/billing/settlements",
        json={"bill_type": "inpatient", "admission_id": admission["id"], "insurance_pay": 0},
        headers=admin,
    ).json()
    assert body["deposit_offset"] == 0.0
    assert body["payable_after_offset"] == 80.0


def test_押金余额不足预警_按缺口排序且阈值参数化(client, admin, ward):
    # 甲：押金 50，未结 160 → gap -110；乙：押金 500，未结 160 → gap 340
    _charge_item(client, admin, "DEP160", 160)
    poor = _admit(client, admin, ward)
    client.post(
        "/api/billing/deposits", json={"admission_id": poor["id"], "amount": 50}, headers=admin
    )
    _bill(client, admin, poor, "DEP160")
    rich = _admit(client, admin, ward)
    client.post(
        "/api/billing/deposits", json={"admission_id": rich["id"], "amount": 500}, headers=admin
    )
    _bill(client, admin, rich, "DEP160")

    alerts = client.get("/api/billing/deposits/alerts", headers=admin).json()
    ids = [a["admission_id"] for a in alerts]
    assert poor["id"] in ids and rich["id"] not in ids, "默认阈值 0：只报押金盖不住未结费用的"
    mine = [a for a in alerts if a["admission_id"] == poor["id"]][0]
    assert mine["balance"] == 50.0 and mine["unsettled"] == 160.0 and mine["gap"] == -110.0

    wide = client.get("/api/billing/deposits/alerts", params={"threshold": 400}, headers=admin).json()
    wide_ids = [a["admission_id"] for a in wide]
    assert poor["id"] in wide_ids and rich["id"] in wide_ids, "调大阈值应把将不足的也提前报出来"
    assert wide_ids.index(poor["id"]) < wide_ids.index(rich["id"]), "预警按缺口从小到大排序"
