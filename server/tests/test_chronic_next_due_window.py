"""手填「下次随访日」限在 [今天, 今天 + 3650 天]；建档的到期日只加上界（P2-1545，第四十五批扫描 AI2-7）。

建档与记随访的 `next_due` 原先只查格式（P2-55），没有上下界。扫描实测（修前代码）：录随访把 2026 敲成 2062，下次到期
2062-10-05 照收 201，今天查、按 2030-01-01 查都不在超期名单里——这位患者从此永不超期；填成去年 2025-10-05 照收 201，刚录完
就在超期名单里。

修法：记随访手填的下次随访日不得早于今天、不得晚于今天起 3650 天——上界与病种随访周期的上限同一个数（P1-96，
`FOLLOWUP_INTERVAL_MAX_DAYS`，两处共用）；建档只查上界（补录存量档案的到期日可以早于今天）。都回 422、不落库；留空照旧按
病种周期自动建议。慢病页随访表单的日期框补 `min` / `max`，与后端一致。
"""
import re
import shutil
from datetime import timedelta

import pytest

from chronic_followup_pages import run
from conftest import business_today

from app.database import SessionLocal
from app.models import ChronicPatient, FollowUp

#: 手填的下次随访日最晚到今天起这么多天（与病种随访周期上限同一个数，下面第一条钉着两处共用一个常量）
MAX_DAYS = 3650


def _day(offset: int) -> str:
    return (business_today() + timedelta(days=offset)).isoformat()


def _beyond(day: str) -> str:
    return f"下次随访日（{day}）不得晚于 {_day(MAX_DAYS)}（今天起 3650 天）"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21545 东镇卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patients = [client.post("/api/patients", headers=admin, json={
        "name": f"P21545 患者{n}", "id_card": f"33010619650101545{n}"}).json()["id"] for n in range(4)]
    return {"org": org, "patients": patients}


def _register(client, admin, world, n, next_due):
    return client.post("/api/chronic", headers=admin, json={
        "patient_id": world["patients"][n], "disease": "hypertension", "managed_by_org_id": world["org"],
        "next_due": next_due})


def _state(chronic_id):
    with SessionLocal() as db:
        return db.get(ChronicPatient, chronic_id).next_due, db.query(FollowUp).filter(
            FollowUp.chronic_id == chronic_id).count()


def test_上界与病种随访周期上限同一个数():
    from app.routers.chronic import FOLLOWUP_INTERVAL_MAX_DAYS, DiseaseTypeCreate, DiseaseTypeUpdate

    assert FOLLOWUP_INTERVAL_MAX_DAYS == MAX_DAYS
    for model in (DiseaseTypeCreate, DiseaseTypeUpdate):
        bounds = [m.le for m in model.model_fields["followup_interval_days"].metadata if getattr(m, "le", None)]
        assert bounds == [FOLLOWUP_INTERVAL_MAX_DAYS], model


def test_记随访_远期与过去的下次随访日422_档案不动(client, admin, world):
    created = _register(client, admin, world, 0, "")
    assert created.status_code == 201, created.text
    cid, due = created.json()["id"], created.json()["next_due"]
    for bad, detail in (
        ("2062-10-05", _beyond("2062-10-05")),                       # 修前 201，永不进超期名单
        (_day(MAX_DAYS + 1), _beyond(_day(MAX_DAYS + 1))),
        (_day(-365), f"下次随访日（{_day(-365)}）不得早于今天"),       # 修前 201，刚录完就超期
        (_day(-1), f"下次随访日（{_day(-1)}）不得早于今天"),
    ):
        resp = client.post(f"/api/chronic/{cid}/followups", headers=admin,
                           json={"sbp": 150, "dbp": 95, "next_due": bad})
        assert resp.status_code == 422, (bad, resp.text)
        assert resp.json() == {"detail": detail}, bad
    assert _state(cid) == (due, 0)
    assert cid not in {c["id"] for c in client.get("/api/chronic/overdue", headers=admin).json()}


def test_记随访_今天与3650天内照收_留空照旧自动建议(client, admin, world):
    cid = _register(client, admin, world, 1, "").json()["id"]
    for good in (_day(0), _day(MAX_DAYS), _day(30)):
        resp = client.post(f"/api/chronic/{cid}/followups", headers=admin,
                           json={"sbp": 150, "dbp": 95, "next_due": good})
        assert resp.status_code == 201, (good, resp.text)
        assert (resp.json()["next_due"], resp.json()["next_due_suggested"]) == (good, False)
    auto = client.post(f"/api/chronic/{cid}/followups", headers=admin, json={"sbp": 150, "dbp": 95, "next_due": ""})
    assert auto.status_code == 201, auto.text
    assert (auto.json()["next_due"], auto.json()["next_due_suggested"]) == (_day(90), True)


def test_建档_早于今天照收_超上界422(client, admin, world):
    past = _register(client, admin, world, 2, "2026-01-15")   # 补录存量档案：到期日早于今天照收
    assert past.status_code == 201 and past.json()["next_due"] == "2026-01-15", past.text
    far = _register(client, admin, world, 3, _day(MAX_DAYS + 1))
    assert far.status_code == 422, far.text   # 修前 201
    assert far.json() == {"detail": _beyond(_day(MAX_DAYS + 1))}
    with SessionLocal() as db:
        assert db.query(ChronicPatient).filter(ChronicPatient.patient_id == world["patients"][3]).count() == 0
    edge = _register(client, admin, world, 3, _day(MAX_DAYS))
    assert edge.status_code == 201 and edge.json()["next_due"] == _day(MAX_DAYS), edge.text


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")
def test_随访表单的日期框_min与max与后端一致(client, admin):
    html = run(client, admin, "admin", "await renderChronic(); return pageHtml();")
    (field,) = re.findall(r'<input name="next_due"[^>]*>', html)
    # 修前日期框没有界：2062 年、去年都点得进去
    assert f'min="{_day(0)}"' in field and f'max="{_day(MAX_DAYS)}"' in field, field
