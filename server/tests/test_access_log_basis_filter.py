"""调阅留痕页的「依据」筛选改成下拉：值是编码、显示中文名（P2-1023，第二十九批「前后端取值表」扫描 E1-5，与 P2-826 同形）。

`list_access_logs` 按依据编码等值比（`AccessLog.basis == basis`），表格与构成图显示的是 `basis_name`（本机构就诊、患者授权……）；
页面的「依据」原先是自由文本框，占位只提示了 `BASIS_NAMES` 11 个码里的两个——稽核员照表格填「本机构就诊」「全域角色」查回空表，
看起来像「没有这类调阅」。修后改成下拉：选项取自全量调阅构成（`/api/access-logs/stats` 不带患者）里出现过的依据，编码作值、
后端给的中文名作显示；按患者聚焦的构成只是一个人的，不拿它收窄下拉。
"""
from pathlib import Path

PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-clinical.js").read_text(encoding="utf-8")


def test_接口按编码比_页面所依赖的依据与中文名都在(client, admin):
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21023 留痕", "id_card": "330106198801011023", "gender": "女"}).json()
    # 按患者查调阅记录本身也留痕、依据记「全域角色」——保证留痕里有这一类
    assert client.get("/api/access-logs", headers=admin, params={"patient_id": patient["id"]}).status_code == 200
    assert client.get("/api/access-logs", headers=admin, params={"basis": "全域角色"}).json() == []   # 照表格填中文：空表
    by_code = client.get("/api/access-logs", headers=admin, params={"basis": "global"}).json()
    assert by_code and {(r["basis"], r["basis_name"]) for r in by_code} == {("global", "全域角色")}
    stats = client.get("/api/access-logs/stats", headers=admin).json()
    assert ("global", "全域角色") in {(b["basis"], b["basis_name"]) for b in stats["by_basis"]}   # 下拉的选项从这里来


def test_页面的依据是下拉_编码作值中文名作显示():
    start = PAGE.index('<form class="inline" id="al-search">')
    form = PAGE[start:PAGE.index("</form>", start)]
    assert '<select name="basis"><option value="">全部依据</option></select>' in form
    assert '<input name="basis"' not in form   # 修前是自由文本框
    start = PAGE.index("const fillBases = (bases) => {")
    fill = PAGE[start:PAGE.index("\n  };\n", start)]
    assert '`<option value="${esc(b.basis)}">${esc(b.basis_name)}</option>`' in fill
    assert "select.value = picked;" in fill   # 重填不丢已选的依据
    assert "if (!patientId) fillBases(st.by_basis);" in PAGE   # 只拿全量构成填，按患者聚焦的不收窄
