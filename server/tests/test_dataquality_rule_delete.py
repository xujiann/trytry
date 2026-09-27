"""数据质控规则库：自建规则能在页面上删，内置规则只能停用（P2-564）。

`DELETE /api/dataquality/rules/{id}` 一直只有接口，页面只有启停与切严重度（动词级孤儿）；种子 docstring 写着「经
/api/dataquality/rules 增删调整」。接口删内置规则照删、回 200，可种子每次启动按编码补缺——删了下次启动原样补回来，
本地对它的停用与严重度调整反倒丢了。

修法：内置规则删除即 409「不用请停用」，清单带 `builtin`（纯加字段）；页面给自建规则加「删除」（先确认），
内置规则标「内置」、没有删除；三个写按钮只给管理员（后端都是 require_admin，别的角色点了只会 403）。
"""
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


def _render_dataquality() -> str:
    source = (STATIC / "pages-public.js").read_text(encoding="utf-8")
    start = source.index("async function renderDataQuality()")
    return source[start:source.index("\nasync function ", start + 1)]


def test_内置规则不能删_自建的能删(client, admin):
    rules = {r["code"]: r for r in client.get("/api/dataquality/rules", headers=admin).json()}
    assert rules["QC001"]["builtin"] is True
    refused = client.delete(f"/api/dataquality/rules/{rules['QC001']['id']}", headers=admin)
    assert refused.status_code == 409, refused.text   # 修前 200，下次启动种子原样补回
    assert "停用" in refused.json()["detail"]
    assert any(r["code"] == "QC001" for r in client.get("/api/dataquality/rules", headers=admin).json())

    created = client.post("/api/dataquality/rules", headers=admin, json={
        "code": "P2564", "name": "P2564 自建规则", "target_table": "patients", "rule_type": "required",
        "config": {"field": "phone"}, "severity": "warn"})
    assert created.status_code == 201, created.text
    assert created.json()["builtin"] is False
    assert client.delete(f"/api/dataquality/rules/{created.json()['id']}", headers=admin).json() == {
        "deleted": created.json()["id"]}


def test_页面删除只给自建规则_先确认_写按钮只给管理员():
    body = _render_dataquality()
    row = body[body.index('${table(["编码", "名称", "类型", "被检表", "严重度", "状态", "操作"], rules'):]
    row = row[:row.index("})}")]
    assert "${canRule ? `<button" in row                                  # 三个写按钮只给管理员
    assert 'r.builtin ? ""' in row and "data-qcdel" in row                # 修前没有删除按钮；内置的不给
    handler = body[body.index('$("#page-body").onclick'):]
    delete_branch = handler[handler.index("if (qcdel)"):handler.index("} else if (qctoggle)")]
    assert delete_branch.index("confirm(") < delete_branch.index('method: "DELETE"')
