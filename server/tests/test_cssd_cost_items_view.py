"""消毒供应成本核算只看得到按批次的合计与构成：逐条登记的成本项（金额、备注）录进去就没处核对（P2-495）。

`GET /api/cssd/cost-items?batch_id=` 一直在，前端一个调用都没有（读动词棘轮 P2-475 登记在册）；录错一笔只能从单件
成本的异常上倒推。

修法：批次表每行一个「成本项」，列出该批次登记的每一项（类型取后端 `cost_type_name`、金额、备注）。
"""
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


def _draw_cssd_costs() -> str:
    source = (STATIC / "pages-public.js").read_text(encoding="utf-8")
    start = source.index("async function drawCssdCosts()")
    return source[start:source.index("\nasync function ", start + 1)]


def test_每个批次都能看成本项():
    body = _draw_cssd_costs()
    assert 'data-costitems="${esc(b.batch_id)}">成本项</button>' in body   # 修前没有这一格
    assert "api(`/api/cssd/cost-items?batch_id=${encodeURIComponent(batchId)}`)" in body
    table = body[body.index('table(["ID", "类型", "金额", "备注"]'):]
    table = table[:table.index("</tr>`)")]
    assert "i.cost_type_name" in table and "esc(i.cost_type)" not in table   # 类型文案取自后端
    for field in ("i.amount", "i.note"):
        assert field in table, field


def test_按批次取成本项(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2495 供应中心", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    batch = client.post("/api/cssd/batches", headers=admin, json={
        "batch_no": "P2495-B1", "center_org_id": org, "item_name": "换药包", "quantity": 20})
    assert batch.status_code == 201, batch.text
    bid = batch.json()["id"]
    for cost_type, amount, note in (("labor", 30, "打包两人"), ("energy", 12.5, "")):
        assert client.post("/api/cssd/cost-items", headers=admin, json={
            "batch_id": bid, "cost_type": cost_type, "amount": amount, "note": note}).status_code == 201
    rows = client.get("/api/cssd/cost-items", headers=admin, params={"batch_id": bid}).json()
    assert sorted((r["cost_type_name"], r["amount"], r["note"]) for r in rows) == [("人工", 30, "打包两人"), ("能耗", 12.5, "")]
