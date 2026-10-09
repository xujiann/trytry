"""慢专病「逐级转诊闭环」管理页每行只给后端当前状态收的动作（P2-580，第十二批「按钮 vs 状态机」扫描 Z2-1）。

原先通过 / 退回 / 到院 / 下转 / 随访接收五个按钮每行都画，清单又含已结束的单子（`open_only=false`）：实测已提交的单子
点到院 / 下转 / 随访接收三个 409，已到院的点通过 / 退回 / 到院三个 409，已闭环 / 已退回 / 已撤回的五个全是 409。医生
移动端早在 P2-101 按状态给了（`spdReferralOps`），管理端没改。

这里不抄一份状态表来对：对每个状态、每个动作真去调一遍接口，页面给不给这个按钮，必须恰好等于后端收不收（非 409）。

P2-794 起页面不再自己按状态摆，改按清单行上后端现算的 `actions`（状态之外还看机构，按机构的那一半见
`test_spd_referral_row_actions.py`）；这里以管理员（机构判据全放行）取 `actions`，对的仍是「状态」这一维。
"""
from pathlib import Path

import pytest

from app.database import SessionLocal
from app.models import Organization, Patient
from app.spd.models import SpdReferralCase

SOURCE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-spd.js").read_text(encoding="utf-8")

STATUSES = ["submitted", "station_reviewed", "township_reviewed", "accepted", "arrived", "down_referred",
            "closed", "rejected", "withdrawn"]


def _row_actions(client, headers, case_id: int) -> list[str]:
    """清单行上后端给的 `actions`（页面按它摆按钮，P2-794）。"""
    rows = client.get("/api/spd/referrals", headers=headers, params={"limit": 200}).json()
    return {r["id"]: r["actions"] for r in rows}[case_id]


@pytest.fixture(scope="module")
def world(client, admin):
    with SessionLocal() as db:
        village = Organization(name="P2580 村卫生室", org_type="village", level="village")
        county = Organization(name="P2580 县医院", org_type="lead_hospital", level="county")
        db.add_all([village, county])
        db.flush()
        patient = Patient(name="P2580 患者", id_card="330127196001012580", ehc_no="EHC-P2580")
        db.add(patient)
        db.flush()
        case = SpdReferralCase(patient_id=patient.id, direction="up", initiator_org_id=village.id,
                               current_org_id=village.id, current_level="village", target_org_id=county.id)
        db.add(case)
        db.commit()
        return {"case": case.id, "county": county.id, "village": village.id}


def _set_status(world, status: str) -> None:
    """持有机构一并拨回村卫生室（P2-1608）：前面某一格下转成功后单子已在县医院手上，再拿县医院当下转目标是「下转给
    自己」，那一格 422 与这里要对的状态维无关。"""
    with SessionLocal() as db:
        case = db.get(SpdReferralCase, world["case"])
        case.status, case.current_org_id = status, world["village"]
        db.commit()


ACTIONS = {
    "review": lambda w: ("review", {"action": "pass"}),
    "arrive": lambda w: ("arrive", {}),
    "down": lambda w: ("down", {"target_org_id": w["county"]}),
    "recv": lambda w: ("receive-followup", {}),
    "withdraw": lambda w: ("withdraw", None),
}


@pytest.mark.parametrize("op", sorted(ACTIONS))
@pytest.mark.parametrize("status", STATUSES)
def test_页面给不给这个动作恰好等于后端收不收(client, admin, world, op, status):
    _set_status(world, status)
    offered = op in _row_actions(client, admin, world["case"])
    path, body = ACTIONS[op](world)
    resp = client.post(f"/api/spd/referrals/{world['case']}/{path}", headers=admin, json=body)
    assert (resp.status_code != 409) == offered, (status, op, resp.status_code, resp.text)
    if offered:
        assert resp.status_code == 200, resp.text


def test_退回与通过同一组状态_撤回只在进上级审核之前():
    from app.spd.routers.referral import _ACTION_STATUSES

    assert _ACTION_STATUSES["withdraw"] == ("submitted", "station_reviewed")   # 后端 withdraw 的守卫（发起人另判）
    row = SOURCE[SOURCE.index("function spdReferralRowOps(c)"):]
    row = row[:row.index("\n}\n")]
    assert "const on = (op) => (c.actions || []).includes(op);" in row   # P2-794：按后端现算的动作摆，不再自己按状态摆
    assert 'on("review") ? `<button class="btn secondary" data-ref-pass=' in row
    assert "data-ref-reject" in row.split('on("arrive")')[0]   # 退回与通过挂同一个条件
    listing = SOURCE[SOURCE.index('table(["ID", "患者", "病种", "方向", "当前层级", "状态", "有效就诊", "操作"]'):]
    assert "<td>${spdReferralRowOps(c)}</td>" in listing[:1200]   # 修前五个按钮无条件画在这里
