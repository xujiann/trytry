"""Westgard 判定按录入的十进制数算 z：压在 ±2SD / ±3SD 线上的测定值不被判超线（P2-156）。

`_westgard` 写明「比较一律用严格大于：z 恰为 ±2.0/±3.0 不触发（超出才算）」；z 却是二进制浮点算的——
(4.2 − 4.0) / 0.1 = 2.0000000000000018、(1.3 − 1.0) / 0.1 = 3.0000000000000004。既有用例只用了靶值 5.0 / SD 0.5，
这组数在二进制里恰好精确，所以一直是绿的；靶值或 SD 带小数的批号，压线的测定值都被判成超线。
"""
import pytest


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post("/api/organizations", headers=admin, json={
        "name": "P2156 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]


def _lot(client, admin, org, lot_no, target, sd):
    resp = client.post("/api/labqc/lots", headers=admin, json={
        "org_id": org, "item_code": "K", "item_name": "血清钾", "lot_no": lot_no, "target_value": target, "sd": sd})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _measure(client, admin, lot, value):
    resp = client.post(f"/api/labqc/lots/{lot}/measurements", headers=admin, json={"value": value})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return body["warning"], body["out_of_control"], body["violated_rules"]


def test_压在2SD线上_不警告_连着两次也不失控_反向压线不算R4s(client, admin, org):
    lot = _lot(client, admin, org, "P2156-A", 4.0, 0.1)
    assert _measure(client, admin, lot, 4.2) == (False, False, "")   # 修前 (True, False, "")：1-2s 警告
    assert _measure(client, admin, lot, 4.2) == (False, False, "")   # 修前失控 2-2s
    assert _measure(client, admin, lot, 3.8) == (False, False, "")   # 修前失控 R-4s（极差 4.0000000000000036）
    assert _measure(client, admin, lot, 4.0) == (False, False, "")
    assert _measure(client, admin, lot, 4.21) == (True, False, "")   # 真超出 2SD 的照旧警告


def test_压在3SD线上_只警告不失控(client, admin, org):
    lot = _lot(client, admin, org, "P2156-B", 1.0, 0.1)
    assert _measure(client, admin, lot, 1.3) == (True, False, "")    # 修前失控 1-3s（z = 3.0000000000000004）
    assert _measure(client, admin, lot, 1.31)[1] is True             # 真超出 3SD 的照旧失控
