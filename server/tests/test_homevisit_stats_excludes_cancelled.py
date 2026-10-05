"""上门统计的「上门工单」数与「签约关联率」不再把已取消的工单算进去（P2-1549，第四十五批扫描 AI1-8）。

修前：`homevisits.visit_stats` 的 `total` 是各状态计数之和、`contract_linked` 数的是所有挂签约的工单——取消的工单从没上过门，
照样计入。扫描实测（`r4_homevisits.py`）：3 张工单里 1 张挂签约已办结、1 张挂签约已取消、1 张没签约，统计回 total 3、
挂签约 2、关联率 66.67%；剔除已取消后应为 2 张、挂签约 1 张、50%。

修法：`total` 与 `contract_linked` 都剔除已取消的，`by_status` 照旧全列（已取消的有几张在那里看），docstring 写明口径；
家医签约页上门服务那张卡片由「上门工单」改写「上门工单（不含已取消）」。是否只数已办结的随 P2-979 另定。
"""
from pathlib import Path

import pytest

from conftest import login

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


@pytest.fixture(scope="module")
def world(client, admin):
    """三张工单：挂签约已办结、挂签约已取消、没签约（已派单）。统计不分机构，本模块的库里只有这三张。"""
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21549 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    resp = client.post("/api/users", headers=admin, json={
        "username": "p21549_doc", "password": "passw0rd1", "role": "doctor", "org_id": org})
    assert resp.status_code == 201, resp.text
    doctor = login(client, "p21549_doc", "passw0rd1")
    patients = [client.post("/api/patients", headers=admin, json={
        "name": f"P21549 居民{i}", "id_card": f"33010219710301{1549 + i:04d}"}).json()["id"] for i in range(3)]
    for pid in patients[:2]:
        signed = client.post("/api/contracts", headers=doctor, json={
            "patient_id": pid, "org_id": org, "doctor_name": "P21549 家庭医生"})
        assert signed.status_code == 201, signed.text
    orders = []
    for pid in patients:   # 不传签约：按患者 + 机构自动关联履约中的签约
        resp = client.post("/api/homevisits", headers=doctor, json={
            "patient_id": pid, "org_id": org, "service_type": "nursing"})
        assert resp.status_code == 201, resp.text
        orders.append(resp.json())
    done, cancelled, unlinked = orders
    assert done["contract_id"] and cancelled["contract_id"] and unlinked["contract_id"] is None
    for oid in (done["id"], unlinked["id"]):
        assert client.post(f"/api/homevisits/{oid}/dispatch", headers=doctor,
                           json={"assignee_name": "P21549 护士"}).status_code == 200
    assert client.post(f"/api/homevisits/{done['id']}/complete", headers=doctor,
                       json={"service_note": "换药"}).status_code == 200
    assert client.post(f"/api/homevisits/{cancelled['id']}/cancel", headers=doctor).status_code == 200
    return {"doctor": doctor}


def test_已取消的不计入上门工单数与签约关联率_状态分布照旧全列(client, world):
    stats = client.get("/api/homevisits/stats", headers=world["doctor"]).json()
    assert stats == {
        "total": 2,                       # 修前 3：取消的那张从没上过门，也算一张上门工单
        "by_status": {"cancelled": 1, "completed": 1, "dispatched": 1},   # 照旧全列，已取消的在这里看
        "contract_linked": 1,             # 修前 2
        "contract_linked_ratio_pct": 50.0,   # 修前 66.67
    }


def test_页面卡片写明不含已取消():
    source = (STATIC / "pages-public.js").read_text(encoding="utf-8")
    body = source[source.index("async function drawHomeVisits("):]
    body = body[:body.index("\n}\n")]
    assert '<div class="label">上门工单（不含已取消）</div><div class="value">${stats.total}</div>' in body
