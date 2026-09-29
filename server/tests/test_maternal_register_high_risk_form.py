"""孕产妇建册表单补「高危」「高危因素」两项（P2-857，第二十三批「页面表单提交的字段与取值 vs 后端请求模型」扫描 Y1-6）。

`MaternalCreate` 早就收 `high_risk` / `risk_factors`；页面建册表单没有这两项，孕产妇记录也只有访视 / 分娩 / 结案三个子路由，
高危只能等产检血压 ≥140 或产筛高风险自动标上——高龄、瘢痕子宫这类建册时就能判出的高危，档案上是「正常」，`high_risk=true`
清单与「高危排前」都漏掉她。修后建册表单补这两项；建册之后的人工标记 / 解除（谁能解除）另行待裁定。
"""
from pathlib import Path

PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-clinical.js").read_text(encoding="utf-8")


def test_建册表单有高危与高危因素():
    start = PAGE.index('<form class="inline" id="mat-form">')
    form = PAGE[start:PAGE.index("</form>", start)]
    assert '<input type="checkbox" name="high_risk">' in form and '<input name="risk_factors"' in form   # 修前都没有
    assert "body.high_risk = e.target.high_risk.checked;" in PAGE


def test_按页面送的高危建册_进高危清单(client, admin):
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2857 孕妇", "id_card": "330102198801012857", "gender": "女", "birth_date": "1988-01-01"}).json()["id"]
    made = client.post("/api/maternal/records", headers=admin, json={
        "patient_id": patient, "gravidity": 2, "parity": 1, "high_risk": True, "risk_factors": "高龄、瘢痕子宫"})
    assert made.status_code in (200, 201), made.text
    rows = client.get("/api/maternal/records", headers=admin, params={"high_risk": "true", "limit": 500}).json()
    assert [(r["high_risk"], r["risk_factors"]) for r in rows if r["patient_id"] == patient] == [(True, "高龄、瘢痕子宫")]
