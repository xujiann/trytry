"""住院页「床位效率」的占床率与决策分析的「床位使用率」不再同名（P2-696，第十七批「比率的分子是不是分母的子集」扫描 U3-7）。

住院统计 `occupancy_pct` 是此刻的占床率（占用床位 ÷ 床位），决策分析运行效率的床位使用率是实际占用床日 ÷（开放床位 ×
期间天数）——口径写死并对外公布。两页的列名都叫「使用率」：10 张床此刻占 9 张、本月 150 床日，住院页 90%、决策分析
50%，同一家院两个「使用率」。字段名不改（向后兼容），住院页列名改「当前占床率」。
"""
from pathlib import Path

from jssrc import strip_comments

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


def test_住院页列名是当前占床率():
    src = strip_comments((STATIC / "pages-clinical.js").read_text(encoding="utf-8"))
    assert 'table(["机构", "床位", "占用", "当前占床率", "在院", "累计出院"]' in src   # 修前「使用率」
    assert '"占用", "使用率"' not in src


def test_接口字段名不变(client, admin):
    resp = client.get("/api/inpatient/stats", headers=admin)
    assert resp.status_code == 200, resp.text
    assert all("occupancy_pct" in row for row in resp.json())
