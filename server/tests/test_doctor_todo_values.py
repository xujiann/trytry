"""医生移动端的待办卡片把取值原样打印：「中心 imaging」「状态 pending」「危急值状态 notified」「机构 3」（P2-371）。

待办卡片按 `Object.entries(row)` 逐键打印，`FIELD_NAMES` 只翻译了键名、取值原样出；同一个文件里检查申请页、危急值页
早有 `CENTER_NAMES` / `EXAM_STATUS` / `CRITICAL_TAGS` 三张取值表。缺药行只带机构编号，卡片上是「机构 3」。

修法：待办的取值按键过一遍本文件现成的取值表；缺药行带上机构名称（`org_name`），卡片显示名称、不显示编号。
"""
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "app" / "static" / "m" / "doctor.js").read_text(encoding="utf-8")


def test_待办卡片的取值过取值表():
    body = SRC[SRC.index("async function loadTodos()"):]
    body = body[:body.index("\n}\n")]
    assert "TODO_VALUES[k]" in body, body   # 修前 kv(FIELD_NAMES[k] || k, esc(v))
    table = SRC[SRC.index("const TODO_VALUES = {"):]
    table = table[:table.index("};")]
    for key, source in (("center_type", "CENTER_NAMES"), ("status", "EXAM_STATUS"),
                        ("critical_status", "CRITICAL_TAGS")):
        assert f"{key}: (v) => " in table and source in table, (key, table)


def test_缺药行显示机构名称(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2371 缺药卫生院", "org_type": "township", "level": "township"}).json()["id"]
    stock = client.post("/api/pharmacy/stocks", headers=admin, json={
        "org_id": org, "drug_code": "P2371", "drug_name": "P2371 片", "quantity": 1, "threshold": 5})
    assert stock.status_code in (200, 201), stock.text
    body = client.get("/api/todos", headers=admin).json()
    rows = next(i for i in body["items"] if i["type"] == "stock_shortage")["list"]
    row = next(r for r in rows if r["org_id"] == org)
    assert row["org_name"] == "P2371 缺药卫生院"   # 修前没有这个键
    assert 'org_name: "机构"' in SRC and '"org_id"' in SRC
