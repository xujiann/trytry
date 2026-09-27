"""中药代煎单、公卫事件、中药制剂批次三页的待办只看「最新一页」：挤出窗口的那条就没有「流转」「处置记录 / 结案」「发放」（P2-457）。

代煎单清单只回最新 200 张、公卫事件最新 100 起、制剂批次最新 200 批，操作按钮只摆在这一页里：一张没送达的代煎单后面
又下了 200 张已送达的，它的「流转」就不见了（`?status=ordered` 才取得到）；一起处置中的事件排在 100 起已结案的之后，
同理。与 P2-456 同一个毛病、同一个修法：待办按状态单独取、排在最前、按 id 去重（core.js `actionableFirst`）。
"""
import os

import pytest

from app.database import SessionLocal

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")


def _function(filename: str, name: str) -> str:
    with open(os.path.join(STATIC, filename), encoding="utf-8") as fh:
        source = fh.read()
    start = source.index(f"async function {name}()")
    end = source.find("\nasync function ", start + 1)
    return source[start:end if end != -1 else len(source)]


@pytest.mark.parametrize("filename, name, fetch, merged", [
    ("pages-clinical.js", "renderTcm",
     '["ordered", "dispensed", "decocted", "delivering"].map((st) => api(`/api/tcm/dispense-orders?status=${st}`))',
     "const orders = actionableFirst(recent, ...open);"),
    ("pages-clinical.js", "renderPublicHealth", 'api("/api/publichealth/events?status=active")',
     "const events = actionableFirst(recent, active);"),
    ("pages-public.js", "drawTcmPreparations", 'api("/api/tcm/preparation-batches?status=produced")',
     "const batches = actionableFirst(recentBatches, produced);"),
])
def test_三页的待办单独取_排在最前(filename, name, fetch, merged):
    body = _function(filename, name)
    assert fetch in body   # 修前只取最新一页
    assert merged in body


def test_代煎单_第201张之前没送达的那张_按状态取得到(client, admin):
    from sqlalchemy import insert

    from app.models import TcmDispenseOrder

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2457 中医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2457 患者", "id_card": "330106197012122457"}).json()["id"]
    with SessionLocal() as db:
        first = TcmDispenseOrder(patient_id=patient, from_org_id=org, herbs="黄芪 30g", doses=7, status="ordered")
        db.add(first)
        db.commit()
        first_id = first.id
        db.execute(insert(TcmDispenseOrder), [{"patient_id": patient, "from_org_id": org, "herbs": "当归 10g",
                                               "doses": 3, "status": "delivered"} for _ in range(200)])
        db.commit()
    recent = [o["id"] for o in client.get("/api/tcm/dispense-orders", headers=admin).json()]
    ordered = client.get("/api/tcm/dispense-orders", headers=admin, params={"status": "ordered"}).json()
    assert first_id not in recent                     # 挤出了最新一页：修前页面上没有它的「流转」
    assert first_id in [o["id"] for o in ordered]
