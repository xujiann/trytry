"""在线咨询、用血申请、上门服务三页的待办行不再只看「最新一页」（P2-408）。

三页都只取一页（在线咨询、用血申请最新 200 条，上门工单最新 100 条），「回复 / 结束」「批准 / 驳回 / 发血」
「派单 / 取消 / 完成」只摆在这一页里：压着没办的那条一被后来的挤出窗口，页面上就再没有一行能办——与审方的待审
队列（P1-148）同一个毛病、同一个修法：待办的按状态单独取一遍（三个清单接口早就支持 `?status=`），排在最前、
按 id 去重。体检的「总检」要先补后端筛选，另见 P2-409。
"""
import os

import pytest

from app.database import SessionLocal
from app.models import HomeVisitOrder, OnlineConsult, TransfusionRequest

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")


def _function(filename: str, name: str) -> str:
    with open(os.path.join(STATIC, filename), encoding="utf-8") as fh:
        source = fh.read()
    start = source.index(f"async function {name}()")
    end = source.find("\nasync function ", start + 1)
    return source[start:end if end != -1 else len(source)]


def test_在线咨询_待回复待结束的单独取_排在最前():
    body = _function("pages-clinical.js", "renderTelemedicine")
    assert 'api("/api/telemedicine/consults?status=open")' in body       # 修前只取最新 200 条
    assert 'api("/api/telemedicine/consults?status=replied")' in body
    assert "const consults = [...open, ...replied, ...recent.filter((c) => !actionableIds.has(c.id))];" in body
    assert 'c.status === "open"' in body and "data-reply" in body and "data-close" in body   # 按钮仍按行状态给


def test_用血申请_待审批待发血的单独取_排在最前():
    body = _function("pages-public.js", "renderBlood")
    assert 'api("/api/blood/requests?status=pending")' in body
    assert 'api("/api/blood/requests?status=approved")' in body
    assert "const requests = [...pending, ...approved, ...recent.filter((r) => !actionableIds.has(r.id))];" in body
    assert 'r.status === "pending"' in body and "data-brev" in body and "data-bissue" in body


def test_上门服务_待派单待完成的单独取_排在最前():
    body = _function("pages-public.js", "drawHomeVisits")
    assert 'api("/api/homevisits?status=applied")' in body
    assert 'api("/api/homevisits?status=dispatched")' in body
    assert "const orders = [...applied, ...dispatched, ...recent.filter((o) => !actionableIds.has(o.id))];" in body
    assert 'o.status === "applied"' in body and "data-hvdis" in body and "data-hvdone" in body


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2408 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2408 患者", "id_card": "330127197309092408"}).json()["id"]
    return {"org": org, "patient": patient}


def _flip(model, row_id, status):
    with SessionLocal() as db:
        db.get(model, row_id).status = status
        db.commit()


def test_三个清单接口按状态筛_页面靠的就是它(client, admin, world):
    """页面取待办靠 `?status=`：筛出来的只能是这一种状态，否则「排在最前」的就不是待办。"""
    consults = [client.post("/api/telemedicine/consults", headers=admin, json={
        "patient_id": world["patient"], "org_id": world["org"], "question": f"P2408 咨询{i}"}).json()["id"]
        for i in range(2)]
    _flip(OnlineConsult, consults[1], "replied")
    got = client.get("/api/telemedicine/consults", headers=admin, params={"status": "open"}).json()
    assert {c["status"] for c in got} == {"open"} and consults[0] in {c["id"] for c in got}

    requests = []
    for _ in range(2):
        created = client.post("/api/blood/requests", headers=admin, json={
            "patient_id": world["patient"], "org_id": world["org"], "blood_type": "A", "component": "rbc",
            "quantity_ml": 200})
        assert created.status_code == 201, created.text
        requests.append(created.json()["id"])
    _flip(TransfusionRequest, requests[1], "approved")
    got = client.get("/api/blood/requests", headers=admin, params={"status": "approved"}).json()
    assert {r["status"] for r in got} == {"approved"} and requests[1] in {r["id"] for r in got}

    orders = []
    for _ in range(2):
        created = client.post("/api/homevisits", headers=admin, json={
            "patient_id": world["patient"], "org_id": world["org"], "service_type": "nursing", "demand": "P2408 换药"})
        assert created.status_code == 201, created.text
        orders.append(created.json()["id"])
    _flip(HomeVisitOrder, orders[1], "dispatched")
    got = client.get("/api/homevisits", headers=admin, params={"status": "dispatched"}).json()
    assert {o["status"] for o in got} == {"dispatched"} and orders[1] in {o["id"] for o in got}
