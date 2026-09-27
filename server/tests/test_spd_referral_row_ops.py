"""慢专病「逐级转诊闭环」管理页每行只给后端当前状态收的动作（P2-580，第十二批「按钮 vs 状态机」扫描 Z2-1）。

原先通过 / 退回 / 到院 / 下转 / 随访接收五个按钮每行都画，清单又含已结束的单子（`open_only=false`）：实测已提交的单子
点到院 / 下转 / 随访接收三个 409，已到院的点通过 / 退回 / 到院三个 409，已闭环 / 已退回 / 已撤回的五个全是 409。医生
移动端早在 P2-101 按状态给了（`spdReferralOps`），管理端没改。

这里不抄一份状态表来对：对每个状态、每个动作真去调一遍接口，页面给不给这个按钮，必须恰好等于后端收不收（非 409）。
"""
import re
from pathlib import Path

import pytest

from app.database import SessionLocal
from app.models import Organization, Patient
from app.spd.models import SpdReferralCase

SOURCE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-spd.js").read_text(encoding="utf-8")

STATUSES = ["submitted", "station_reviewed", "township_reviewed", "accepted", "arrived", "down_referred",
            "closed", "rejected", "withdrawn"]


def _page_ops() -> dict[str, set[str]]:
    block = re.search(r"const SPD_REF_OPS = \{(.*?)\};", SOURCE, re.S)
    assert block, "pages-spd.js 里找不到 SPD_REF_OPS"
    return {op: set(re.findall(r'"(\w+)"', statuses)) for op, statuses in re.findall(r"(\w+): \[([^\]]*)\]", block.group(1))}


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
        return {"case": case.id, "county": county.id}


def _set_status(case_id: int, status: str) -> None:
    with SessionLocal() as db:
        db.get(SpdReferralCase, case_id).status = status
        db.commit()


ACTIONS = {
    "review": lambda w: ("review", {"action": "pass"}),
    "arrive": lambda w: ("arrive", {}),
    "down": lambda w: ("down", {"target_org_id": w["county"]}),
    "recv": lambda w: ("receive-followup", {}),
}


@pytest.mark.parametrize("op", sorted(ACTIONS))
@pytest.mark.parametrize("status", STATUSES)
def test_页面给不给这个动作恰好等于后端收不收(client, admin, world, op, status):
    _set_status(world["case"], status)
    path, body = ACTIONS[op](world)
    resp = client.post(f"/api/spd/referrals/{world['case']}/{path}", headers=admin, json=body)
    offered = status in _page_ops()[op]
    assert (resp.status_code != 409) == offered, (status, op, resp.status_code, resp.text)
    if offered:
        assert resp.status_code == 200, resp.text


def test_退回与通过同一组状态_撤回只在进上级审核之前():
    ops = _page_ops()
    assert ops["withdraw"] == {"submitted", "station_reviewed"}   # 后端 withdraw 的守卫（发起人另判）
    row = SOURCE[SOURCE.index("function spdReferralRowOps(c)"):]
    row = row[:row.index("\n}\n")]
    assert 'on("review") ? `<button class="btn secondary" data-ref-pass=' in row
    assert "data-ref-reject" in row.split('on("arrive")')[0]   # 退回与通过挂同一个条件
    listing = SOURCE[SOURCE.index('table(["ID", "患者", "病种", "方向", "当前层级", "状态", "有效就诊", "操作"]'):]
    assert "<td>${spdReferralRowOps(c)}</td>" in listing[:1200]   # 修前五个按钮无条件画在这里
