"""传染病报告卡的及时性一格写「及时（迟 1 天）」（P2-470）。

`days_late` 是发病到报告隔了几天（与未及时上报清单同口径），不是超出法定时限几天：限 24 小时报告的肺结核昨天发病、
今天报告，`days_late=1, late=false`，卡片却拼成「及时（迟 1 天）」。改写成「发病后 N 天报告」——及时、迟报都说得通。
"""
import os

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")


def test_及时性一格不再把发病到报告的天数写成迟几天():
    with open(os.path.join(STATIC, "pages-clinical.js"), encoding="utf-8") as fh:
        source = fh.read()
    assert "（迟 ${c.days_late} 天）" not in source   # 修前
    assert "（发病后 ${c.days_late} 天报告）" in source


def test_接口端_限24小时的病种昨天发病今天报告_及时且隔1天(client, admin):
    from datetime import timedelta

    from app import clock

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2470 报卡医院", "org_type": "township", "level": "township"}).json()["id"]
    diseases = client.get("/api/infectious/diseases", headers=admin).json()
    within_day = next(d for d in diseases if d["report_hours"] == 24)
    yesterday = (clock.today() - timedelta(days=1)).isoformat()
    case = client.post("/api/infectious/cases", headers=admin, json={
        "org_id": org, "disease_code": within_day["code"], "disease_name": within_day["name"],
        "onset_date": yesterday})
    assert case.status_code == 201, case.text
    card = client.get(f"/api/infectious/cases/{case.json()['id']}/report-card", headers=admin).json()
    assert (card["days_late"], card["late"]) == (1, False)   # 卡上原先拼成「及时（迟 1 天）」
