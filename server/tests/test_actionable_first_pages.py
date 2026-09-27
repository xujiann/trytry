"""远程会诊、双向转诊、病理标本、门诊告知书四页的待办只看「最新一页」：压着没办的那条一被挤出窗口，页面上就再没有一行能办（P2-456）。

四页都只取一页（会诊、转诊最新 200 条，标本最新 500 个，告知书最新 50 份），「受理 / 出意见」「接诊 / 结案」「核收 / 拒收 /
推进」「签署 / 拒签」只摆在这一页里——与审方（P1-148）、咨询 / 用血 / 上门（P2-408）同一个毛病。实测：第 201 张转诊单之前
那张待接诊的不在页面上（`?status=pending` 取得到）；51 份告知书时最早那份待签的不在，而那次就诊的完整度还数着它「待签署」。
修法同它们：待办按状态单独取一遍、排在最前、按 id 去重；四页共用 core.js 的 `actionableFirst`。
"""
import os

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
])
def test_四页的待办单独取_排在最前(filename, name, fetches, merged):
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
