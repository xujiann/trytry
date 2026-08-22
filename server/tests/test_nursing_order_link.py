"""护理记录联动住院医嘱（P1-24a）：关联创建、跨住院拒绝、医嘱执行视图含护理计数。

联动是显式外键（nursing_records.inpatient_order_id）而不是时间猜测；
挂错住院的联动比不联动更糟——质控会拿它下结论，所以跨住院一律 422。
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
def ward(client, admin):
    org = client.post(
        "/api/organizations",
        json={"name": "护理联动县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    return client.post(
        "/api/inpatient/wards", json={"org_id": org["id"], "name": "联动一病区"}, headers=admin
    ).json()


_seq = [0]


def _admit(client, admin, ward):
    k = _seq[0]
    _seq[0] += 1
    patient = client.post(
        "/api/patients",
        json={"name": f"联动患者{k}", "id_card": f"3300001990010177{k:02d}"},
        headers=admin,
    ).json()
    bed = client.post(
        "/api/inpatient/beds", json={"ward_id": ward["id"], "bed_no": f"N-{k:02d}"}, headers=admin
    ).json()
    resp = client.post(
        "/api/inpatient/admissions",
        json={"patient_id": patient["id"], "ward_id": ward["id"], "bed_id": bed["id"]},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _order(client, admin, admission):
    resp = client.post(
        "/api/inpatient/orders",
        json={"admission_id": admission["id"], "order_type": "long", "content": "头孢曲松 2g qd"},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_护理记录关联医嘱创建成功(client, admin, ward):
    admission = _admit(client, admin, ward)
    order = _order(client, admin, admission)
    resp = client.post(
        f"/api/inpatient/admissions/{admission['id']}/nursing-records",
        json={"content": "输注中巡视，无不良反应", "inpatient_order_id": order["id"]},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["inpatient_order_id"] == order["id"]
    # 列表视图透出关联，供质控核对
    rows = client.get(
        f"/api/inpatient/admissions/{admission['id']}/nursing-records", headers=admin
    ).json()
    assert [r["inpatient_order_id"] for r in rows] == [order["id"]]


def test_不关联医嘱的护理记录仍照常创建(client, admin, ward):
    """可空语义：日常巡视本来就不对应医嘱，不传关联必须与联动上线前行为一致。"""
    admission = _admit(client, admin, ward)
    resp = client.post(
        f"/api/inpatient/admissions/{admission['id']}/nursing-records",
        json={"content": "晨间巡视"},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["inpatient_order_id"] is None


def test_跨住院医嘱关联拒绝422(client, admin, ward):
    admission_a = _admit(client, admin, ward)
    admission_b = _admit(client, admin, ward)
    order_b = _order(client, admin, admission_b)
    resp = client.post(
        f"/api/inpatient/admissions/{admission_a['id']}/nursing-records",
        json={"content": "挂错住院的联动", "inpatient_order_id": order_b["id"]},
        headers=admin,
    )
    assert resp.status_code == 422, resp.text
    assert "不属于本次住院" in resp.json()["detail"]


def test_医嘱不存在关联拒绝422(client, admin, ward):
    admission = _admit(client, admin, ward)
    resp = client.post(
        f"/api/inpatient/admissions/{admission['id']}/nursing-records",
        json={"content": "关联不存在的医嘱", "inpatient_order_id": 999999},
        headers=admin,
    )
    assert resp.status_code == 422, resp.text


def test_医嘱执行视图附关联护理记录数(client, admin, ward):
    admission = _admit(client, admin, ward)
    order = _order(client, admin, admission)
    # 先登记一次执行：尚无护理联动，计数为 0
    first = client.post(
        f"/api/inpatient/orders/{order['id']}/executions",
        json={"note": "首剂执行"},
        headers=admin,
    )
    assert first.status_code == 201, first.text
    assert first.json()["nursing_record_count"] == 0
    # 挂两条护理记录后，执行视图的医嘱级计数跟上
    for note in ("皮试后观察 30 分钟", "输注完毕拔针"):
        assert client.post(
            f"/api/inpatient/admissions/{admission['id']}/nursing-records",
            json={"content": note, "inpatient_order_id": order["id"]},
            headers=admin,
        ).status_code == 201
    rows = client.get(f"/api/inpatient/orders/{order['id']}/executions", headers=admin).json()
    assert len(rows) == 1
    assert rows[0]["nursing_record_count"] == 2
