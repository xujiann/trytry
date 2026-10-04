"""医废页的滞留预警面板逐包列出、就地交接（P2-1309，第三十八批扫描 AB1-1）。

修前：医废页（`core.js::renderMedwaste`）取了滞留预警接口（`GET /api/medwaste/alerts`，逐包给追溯码、暂存点、超期天数），
面板上却只印条数和一句「收集超过2天仍未交接」；「滞留」标签与「交接」按钮只摆在主清单里，而主清单是 `GET /api/medwaste`
缺省的最新 500 包、按编号倒序——滞留的恰是最早收的那批，最先被挤出窗口。实测 1 包 5 天前收集未交接、之后又收了 500 包：
驾驶舱横幅「医废滞留 1」、下钻 1 条跳到医废页，预警接口给出这一包（超期 3 天），页面主清单 500 行（X-Total-Count 501）里
没有它，滞留面板也说不出是哪一包、在哪间暂存间，交接只能按号直接调接口。`overdue_alerts` 的 docstring（D-8）写明「只报
"某机构有 3 包超期"而不报是哪几包、在哪间暂存间，等于没报」。

修法：滞留面板按预警接口逐包列出（追溯码、暂存点、超期天数），每包带与主清单同一个 `data-hand` 的「交接」按钮，
走同一套弹窗与接口；文本一律 `esc()`。
"""
import os
from datetime import timedelta

import pytest

from app import clock
from app.database import SessionLocal

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")


def _function(filename: str, name: str) -> str:
    with open(os.path.join(STATIC, filename), encoding="utf-8") as fh:
        source = fh.read()
    start = source.index(f"async function {name}()")
    end = source.find("\nasync function ", start + 1)
    return source[start:end if end != -1 else len(source)]


def _overdue_panel() -> str:
    body = _function("core.js", "renderMedwaste")
    start = body.index("${alerts.length ? panel(`⚠ 滞留预警（${alerts.length}）`")
    return body[start:body.index(') : ""}', start)]


def test_滞留面板按预警接口逐包列出():
    panel = _overdue_panel()
    assert "table([" in panel and "alerts, (w) =>" in panel   # 修前面板正文只有一句说明，没有一行
    assert 'esc(w.trace_code || "—")' in panel                  # 追溯码
    assert 'esc(locationName.get(w.storage_location_id) || "—")' in panel   # 暂存在哪一间（按点位台账对名称）
    assert "${w.overdue_days} 天" in panel                       # 超期天数


def test_滞留面板每包带交接按钮_与主清单同一套动作():
    panel = _overdue_panel()
    assert '<button class="btn secondary" data-hand="${w.id}">交接</button>' in panel
    body = _function("core.js", "renderMedwaste")
    # 主清单与滞留面板的交接按钮都由同一个委托处理：弹窗收转运人、调交接接口、重画
    assert body.count('data-hand="${w.id}"') == 2
    assert 'hand = el("data-hand")' in body
    assert 'await spdModal("医废交接"' in body
    assert "await api(`/api/medwaste/${hand.dataset.hand}/handover`" in body


def test_暂存点名称按含停用点位的台账对():
    body = _function("core.js", "renderMedwaste")
    assert 'api("/api/medwaste/locations?include_inactive=true")' in body
    assert "const locationName = new Map(locations.map((l) => [l.id, l.name]));" in body


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21309 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    storage = client.post("/api/medwaste/locations", headers=admin, json={
        "org_id": org, "name": "P21309 东楼暂存间", "location_type": "storage"})
    assert storage.status_code == 201, storage.text
    return {"org": org, "storage": storage.json()["id"]}


def test_最早收的滞留包挤出最新500包_预警接口逐包给得出(client, admin, world):
    """页面滞留面板靠预警接口逐包列：主清单缺省只回最新 500 包，最早收的那包滞留医废不在里面。"""
    from sqlalchemy import insert

    from app.models import MedicalWaste

    today = clock.today()
    old = client.post("/api/medwaste", headers=admin, json={
        "org_id": world["org"], "waste_type": "infectious", "weight_kg": 1.5,
        "collected_date": (today - timedelta(days=5)).isoformat()})
    assert old.status_code == 201, old.text
    stored = client.post(f"/api/medwaste/{old.json()['id']}/store", headers=admin,
                         json={"storage_location_id": world["storage"]})
    assert stored.status_code == 200, stored.text
    with SessionLocal() as db:
        db.execute(insert(MedicalWaste), [{
            "org_id": world["org"], "waste_type": "infectious", "weight_kg": 0.5, "status": "handed_over",
            "collected_date": today.isoformat(), "trace_code": f"P21309-NEW-{i:04d}"} for i in range(500)])
        db.commit()

    page = client.get("/api/medwaste", headers=admin)          # 页面主清单原样调用
    assert page.headers["X-Total-Count"] == "501"
    assert old.json()["id"] not in {w["id"] for w in page.json()}   # 修前页面上没有它的行，也就没有交接按钮
    (alert,) = [a for a in client.get("/api/medwaste/alerts", headers=admin).json() if a["id"] == old.json()["id"]]
    assert alert["trace_code"] == old.json()["trace_code"]
    assert (alert["storage_location_id"], alert["overdue_days"]) == (world["storage"], 3)

    done = client.post(f"/api/medwaste/{alert['id']}/handover", headers=admin, json={"handler_name": "P21309 转运员"})
    assert done.status_code == 200, done.text
    assert alert["id"] not in {a["id"] for a in client.get("/api/medwaste/alerts", headers=admin).json()}
