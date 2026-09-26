"""全平台规则总目录先截断再计数：用药规则多于 500 条时，目录上的「prescription」与总数都少算（P2-230）。

`GET /api/rules/catalog` 把五路规则并进一张表，用药规则那一路 `.limit(500)` 取前 500 条，`by_source` 与 `total`
却是数这张表的行数——导入了 1500 条用药规则的库（`POST /api/prescriptions/rules/import`），目录上写
「prescription 500」、总数少一千条。管理员照着目录核对规则导全没有，只会以为少导了（与 P1-149 薪资合计、
P2-197 批次追溯合计同一个「先截断再汇总」的形状）。

修法：表里照旧只列前 500 条（按药品编码排），计数按全量 SQL `count()`；页面在列出条数少于总数时注明。
"""
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


def test_用药规则多于上限_表里只列前几百条_计数按全量(client, admin):
    from app.database import SessionLocal
    from app.models import DrugRule
    from app.routers.rules import CATALOG_RX_LIMIT

    codes = [f"P2230-{i:04d}" for i in range(CATALOG_RX_LIMIT + 1)]
    with SessionLocal() as db:
        before = db.query(DrugRule).filter(DrugRule.active.is_(True)).count()
        db.add_all([DrugRule(drug_code=code, max_daily_dose=1, dose_unit="mg") for code in codes])
        db.commit()
    try:
        body = client.get("/api/rules/catalog", headers=admin).json()
        listed = [e for e in body["entries"] if e["source"] == "prescription"]
        assert len(listed) == CATALOG_RX_LIMIT   # 表里照旧只列前 500 条
        assert [e["key"] for e in listed] == sorted(e["key"] for e in listed)   # 按药品编码排
        assert body["by_source"]["prescription"] == before + len(codes)   # 修前 500
        assert body["total"] == sum(body["by_source"].values())   # 修前少算 before + 1 条
        assert len(body["entries"]) == body["total"] - (before + len(codes) - CATALOG_RX_LIMIT)
    finally:
        with SessionLocal() as db:
            db.query(DrugRule).filter(DrugRule.drug_code.in_(codes)).delete(synchronize_session=False)
            db.commit()


def test_没超上限的照旧_计数与表里一致(client, admin):
    body = client.get("/api/rules/catalog", headers=admin).json()
    counted: dict[str, int] = {}
    for entry in body["entries"]:
        counted[entry["source"]] = counted.get(entry["source"], 0) + 1
    assert body["by_source"] == counted and body["total"] == len(body["entries"])


def test_页面在列出条数少于总数时注明():
    source = (STATIC / "pages-mgmt.js").read_text(encoding="utf-8")
    start = source.index("async function renderRules()")
    body = source[start: source.index("\nasync function ", start + 1)]
    assert "catalog.entries.length < catalog.total" in body
    assert "上面的分来源计数是全量" in body
