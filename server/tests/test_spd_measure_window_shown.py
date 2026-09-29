"""居民端「监测」页与医护端「看趋势」说出取数窗口，空了给看更早的入口（P2-830，第二十二批「页面查询参数 vs 后端」扫描 X4-7）。

居民端监测页取 `measurements?limit=30`、不带 days，接口缺省只回近 90 天，空了却说「还没有记录，先添加一条吧」——最后一次
测量在 90 天以前的居民，首页「最新指标」照样有数（不限时间），这一页说没有。医护端「看趋势」同样缺省 90 天，响应和页面
都没说窗口多大，是一张空图。修后趋势出参回显 `days`、页面写明「近 N 天」；居民端空了给「看近两年的」（接口上限 730 天）。
"""
from pathlib import Path

B = "/api/spd"
ROOT = Path(__file__).resolve().parents[1] / "app" / "static"
MOBILE = (ROOT / "m" / "m.js").read_text(encoding="utf-8")
PAGE = (ROOT / "pages-spd.js").read_text(encoding="utf-8")


def test_趋势出参回显窗口(client, admin):
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2830 患者", "id_card": "330106196606062830"}).json()["id"]
    got = client.get(f"{B}/measurements/trend", headers=admin, params={"patient_id": patient, "metric": "bp_sys"})
    assert got.status_code == 200 and got.json()["days"] == 90, got.text   # 修前没有这个键
    got = client.get(f"{B}/measurements/trend", headers=admin, params={
        "patient_id": patient, "metric": "bp_sys", "days": 9999})
    assert got.json()["days"] == 730   # 上限照旧，回显的是实际用的窗口


def test_居民端写明窗口_空了给看近两年的():
    start = MOBILE.index("async function renderSpdMeasure(box, days = 90) {")
    page = MOBILE[start:MOBILE.index("\n}\n", start)]
    assert "spdQuery({ limit: 30, days })" in page   # 修前不带 days
    assert "没有记录 <button type=\"button\" class=\"ghost-btn\" data-spd-older>看近两年的</button>" in page
    assert "renderSpdMeasure(box, 730)" in page
    assert "还没有记录，先添加一条吧" not in page   # 修前空了就这么说


def test_医护端看趋势写明窗口():
    assert '<p class="desc">近 ${t.days} 天${t.points.length ? "" :' in PAGE
