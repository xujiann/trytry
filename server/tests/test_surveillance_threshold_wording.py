"""症候群预警「达到阈值」就报，页面按接口口径写「达到阈值」、并把症候群的口径说明摆出来（P2-695，第十七批「阈值边界 vs 文案」扫描 U2-4）。

接口判的是 `例数 >= 阈值`，多点触发预警的口径写「上报值达到该机构自设阈值即列出」；页面的小标题却是「症候群超阈值」、
日报的标签是「超阈值」——阈值 8 报了 8 例标成「超阈值」，8 并没有超过 8。页面只摆了病原那一路的口径说明，症候群
这一路的没摆。修后页面与接口同一个说法，判定不变（例数等于阈值照报）。
"""
from pathlib import Path

from jssrc import strip_comments

PAGE = strip_comments((Path(__file__).resolve().parents[1] / "app" / "static" / "pages-clinical.js").read_text(encoding="utf-8"))


def test_页面按接口口径写达到阈值():
    assert "超阈值" not in PAGE   # 修前小标题与标签各一处
    assert "<h4>症候群达到阈值</h4>" in PAGE and '<span class="tag danger">达到阈值</span>' in PAGE
    assert "${esc(alerts.caliber.syndrome)}" in PAGE   # 修前只摆了病原的口径


def test_例数等于阈值照报_口径写的是达到(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2695 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    resp = client.post("/api/surveillance/syndromes", headers=admin, json={
        "org_id": org, "syndrome": "fever", "case_count": 8, "threshold": 8, "record_date": "2026-09-27"})
    assert resp.status_code in (200, 201), resp.text
    assert resp.json()["alert"] is True
    alerts = client.get("/api/surveillance/alerts", headers=admin, params={"today": "2026-09-27"}).json()
    assert any(a["org_id"] == org and a["case_count"] == 8 for a in alerts["syndrome_alerts"])
    assert "达到" in alerts["caliber"]["syndrome"]
