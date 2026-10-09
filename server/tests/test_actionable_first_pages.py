"""远程会诊、双向转诊、病理标本、门诊告知书四页的待办只看「最新一页」：压着没办的那条一被挤出窗口，页面上就再没有一行能办（P2-456）。

四页都只取一页（会诊、转诊最新 200 条，标本最新 500 个，告知书最新 50 份），「受理 / 出意见」「接诊 / 结案」「核收 / 拒收 /
推进」「签署 / 拒签」只摆在这一页里——与审方（P1-148）、咨询 / 用血 / 上门（P2-408）同一个毛病。实测：第 201 张转诊单之前
那张待接诊的不在页面上（`?status=pending` 取得到）；51 份告知书时最早那份待签的不在，而那次就诊的完整度还数着它「待签署」。
修法同它们：待办按状态单独取一遍、排在最前、按 id 去重；四页共用 core.js 的 `actionableFirst`。

共享诊断中心与缺药登记两页同族漏修（P2-1310，第三十八批扫描 AB1-2）：共享诊断中心（`renderExams`）只取 `/api/exams` 最新
200 张，铃铛「待诊断申请 N」数的是待诊断 + 诊断中的全量（`todos._pending_exams`）——全县新开 200 张以上之后，早开、还没诊断
的那张页面上没有行，「领取 / 出报告」够不着（实测 201 张时最早那张待诊断的不在页面上，按号直接领取 200）；缺药登记
（`renderMedication`）只取最新 200 条，「在途」卡片数的是已登记 + 采购中的全量（`medication.shortage_stats`），已配送的正等着
结案（已取药 / 未取药只能在已配送之后判）——还没结案的老登记被后来结案的挤出窗口，「流转 / 结案」够不着。修法同上：两页的待办
按状态单独取一遍、`actionableFirst` 排在最前；缺药登记取的是还没结案的三种。
"""
import os
import re

import pytest

from app.database import SessionLocal

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")


def _source(filename: str) -> str:
    with open(os.path.join(STATIC, filename), encoding="utf-8") as fh:
        return fh.read()


def _function(filename: str, name: str) -> str:
    source = _source(filename)
    start = source.index(f"async function {name}()")
    end = source.find("\nasync function ", start + 1)
    return source[start:end if end != -1 else len(source)]


def test_共用的合并函数_待办在前_按id去重():
    core = _source("core.js")
    assert "function actionableFirst(recent, ...actionable) {" in core
    assert "return [...first, ...recent.filter((r) => !ids.has(r.id))];" in core


@pytest.mark.parametrize("filename, name, fetches, merged", [
    ("core.js", "renderConsultations",
     ['api("/api/consultations?status=applied")', 'api("/api/consultations?status=accepted")'],
     "const consultations = actionableFirst(recent, applied, accepted);"),
    ("core.js", "renderReferrals",
     ['api("/api/referrals?status=pending")', 'api("/api/referrals?status=accepted")'],
     "const referrals = actionableFirst(recent, pending, accepted);"),
    ("pages-clinical.js", "renderPathology",
     ['["pending", "received", "embedded", "slided"].map((st) => api(`/api/pathology/specimens?status=${st}`))'],
     "const specimens = actionableFirst(recent, ...open);"),
    ("pages-mgmt.js", "renderOutpatientDocs",
     ['api("/api/outpatient/consents?status=pending&limit=500")'],
     "const consents = actionableFirst(recentConsents, pendingConsents);"),
    # P2-1310：待诊断 + 诊断中 = 铃铛「待诊断申请」；已登记 + 采购中 = 「在途」卡片，加上等着结案的已配送
    # P2-1711：共享诊断页这两种改为续页取全（按状态取也只回缺省一页 200 张）
    ("core.js", "renderExams",
     ['fetchAllPages(api, "/api/exams?status=pending")', 'fetchAllPages(api, "/api/exams?status=diagnosing")'],
     "const requests = actionableFirst(recent, pending, diagnosing);"),
    ("pages-clinical.js", "renderMedication",
     ['api("/api/medication/shortages?status=registered")', 'api("/api/medication/shortages?status=purchasing")',
      'api("/api/medication/shortages?status=delivered")'],
     "const shortages = actionableFirst(recent, registered, purchasing, delivered);"),
])
def test_各页的待办单独取_排在最前(filename, name, fetches, merged):
    body = _function(filename, name)
    for fetch in fetches:
        assert fetch in body, fetch   # 修前只取最新一页
    assert merged in body


@pytest.fixture(scope="module")
def world(client, admin):
    orgs = [client.post("/api/organizations", headers=admin, json={
        "name": f"P2456 {name}", "org_type": otype, "level": level}).json()["id"]
        for name, otype, level in (("卫生院", "township", "township"), ("县医院", "lead_hospital", "county"))]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2456 患者", "id_card": "330106197010102456"}).json()["id"]
    return {"from": orgs[0], "to": orgs[1], "patient": patient}


def test_转诊_第201张之前的待接诊单_按状态取得到(client, admin, world):
    """页面取待办靠 `?status=`：默认清单只回最新 200 张，最早那张待接诊的在按状态取的结果里。"""
    from sqlalchemy import insert

    from app.models import Referral, User

    first = client.post("/api/referrals", headers=admin, json={
        "patient_id": world["patient"], "from_org_id": world["from"], "to_org_id": world["to"], "direction": "up"})
    assert first.status_code == 201, first.text
    with SessionLocal() as db:
        author = db.query(User).filter(User.username == "admin").one().id
        db.execute(insert(Referral), [{
            "patient_id": world["patient"], "from_org_id": world["from"], "to_org_id": world["to"],
            "direction": "up", "reason": "", "status": "completed", "created_by": author} for _ in range(200)])
        db.commit()
    recent = [r["id"] for r in client.get("/api/referrals", headers=admin).json()]
    pending = client.get("/api/referrals", headers=admin, params={"status": "pending"}).json()
    assert first.json()["id"] not in recent                        # 挤出了最新一页：修前页面上没有它
    assert first.json()["id"] in [r["id"] for r in pending]
    assert {r["status"] for r in pending} == {"pending"}


def test_共享诊断中心_第201张之前的待诊断单_按状态取得到(client, admin, world):
    """P2-1310：铃铛「待诊断申请」数着的那张早开的单，默认清单（最新 200 张）里没有，按状态取得到。"""
    from sqlalchemy import insert

    from app.models import ExamRequest, User

    first = client.post("/api/exams", headers=admin, json={
        "patient_id": world["patient"], "from_org_id": world["from"], "center_type": "imaging",
        "item_code": "P21310-DR", "item_name": "P21310 胸部DR"})
    assert first.status_code == 201, first.text
    with SessionLocal() as db:
        author = db.query(User).filter(User.username == "admin").one().id
        db.execute(insert(ExamRequest), [{
            "patient_id": world["patient"], "from_org_id": world["from"], "center_type": "ecg", "item_code": "ECG",
            "item_name": "P21310 心电图", "status": "reported", "created_by": author} for _ in range(200)])
        db.commit()
    bell = {s["type"]: s for s in client.get("/api/todos", headers=admin).json()["items"]}
    assert first.json()["id"] in [r["id"] for r in bell["exam_diagnosis"]["list"]]   # 铃铛数着它
    recent = [r["id"] for r in client.get("/api/exams", headers=admin).json()]
    pending = client.get("/api/exams", headers=admin, params={"status": "pending"}).json()
    assert first.json()["id"] not in recent                        # 挤出了最新一页：修前页面上没有它、领取不了
    assert first.json()["id"] in [r["id"] for r in pending]
    assert {r["status"] for r in pending} == {"pending"}


def test_缺药登记_第201条之前的采购中与已配送登记_按状态取得到(client, admin, world):
    """P2-1310：「在途」卡片数着的那条老登记、等着结案的那条已配送，默认清单（最新 200 条）里都没有，按状态取得到；页面单独取的
    正是还没结案的三种状态。"""
    from sqlalchemy import insert

    from app.models import DrugShortage
    from app.routers.medication import _SHORTAGE_CLOSED, _SHORTAGE_SHORT, SHORTAGE_STATUS_NAMES

    first = client.post("/api/medication/shortages", headers=admin, json={
        "org_id": world["from"], "drug_code": "P21310-INS", "drug_name": "P21310 胰岛素", "quantity": 2})
    assert first.status_code == 201, first.text
    assert client.post(f"/api/medication/shortages/{first.json()['id']}/advance", headers=admin).status_code == 200
    arrived = client.post("/api/medication/shortages", headers=admin, json={
        "org_id": world["from"], "drug_code": "P21310-MET", "drug_name": "P21310 二甲双胍", "quantity": 1})
    assert arrived.status_code == 201, arrived.text
    for _ in range(2):   # 已登记 → 采购中 → 已配送：药到了，等着判已取药 / 未取药
        assert client.post(f"/api/medication/shortages/{arrived.json()['id']}/advance", headers=admin).status_code == 200
    with SessionLocal() as db:
        db.execute(insert(DrugShortage), [{
            "org_id": world["from"], "drug_code": f"P21310-{i}", "drug_name": f"P21310 药{i}", "quantity": 1,
            "status": "collected" if i % 2 else "cancelled"} for i in range(200)])
        db.commit()
    assert client.get("/api/medication/shortages/stats", headers=admin).json()["in_transit"] == 1   # 卡片数着它
    recent = [r["id"] for r in client.get("/api/medication/shortages", headers=admin).json()]
    purchasing = client.get("/api/medication/shortages", headers=admin, params={"status": "purchasing"}).json()
    delivered = client.get("/api/medication/shortages", headers=admin, params={"status": "delivered"}).json()
    assert first.json()["id"] not in recent                        # 挤出了最新 200 条：修前页面上没有它、流转不了
    assert arrived.json()["id"] not in recent                      # 同上：药到了却结不了案
    assert first.json()["id"] in [r["id"] for r in purchasing]
    assert {r["status"] for r in purchasing} == {"purchasing"}
    assert arrived.json()["id"] in [r["id"] for r in delivered]
    # 页面单独取的是还没结案的全部状态（在途 `_SHORTAGE_SHORT` 加上等着结案的已配送）：后端加一个状态而页面没跟，这里先红
    page = _function("pages-clinical.js", "renderMedication")
    fetched = set(re.findall(r'api\("/api/medication/shortages\?status=(\w+)"\)', page))
    assert fetched == set(SHORTAGE_STATUS_NAMES) - _SHORTAGE_CLOSED
    assert set(_SHORTAGE_SHORT) < fetched
