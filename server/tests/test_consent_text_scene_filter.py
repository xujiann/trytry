"""同意文本版本库的「场景」筛选改成下拉：值是编码、显示中文名（P2-826，第二十二批「页面查询参数 vs 后端」扫描 X4-10）。

`list_consent_texts` 按场景编码等值比（`ConsentText.scene == scene`），表格显示的是 `scene_name`（建档、随访……）；页面的
「场景」原先是自由文本框——照表格填「建档」「随访」查回空表，只有填 `archive` 这种编码才查得到，容易让人以为这个场景还
没有文本。修后改成下拉：选项取自版本库里有的场景（含已停用的版本），编码作值、后端给的中文名作显示。
"""
from pathlib import Path

PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-clinical.js").read_text(encoding="utf-8")


def test_接口按编码比_页面所依赖的场景与中文名都在(client, admin):
    assert client.get("/api/consents/texts", headers=admin, params={"scene": "建档"}).json() == []   # 照表格填中文：空表
    by_code = client.get("/api/consents/texts", headers=admin, params={"scene": "archive"}).json()
    assert by_code and {(t["scene"], t["scene_name"]) for t in by_code} == {("archive", "建档")}
    everything = client.get("/api/consents/texts", headers=admin, params={"active_only": "false"}).json()
    assert ("archive", "建档") in {(t["scene"], t["scene_name"]) for t in everything}   # 下拉的选项从这里来


def test_页面的场景是下拉_编码作值中文名作显示():
    start = PAGE.index('<form class="inline" id="tx-filter">')
    form = PAGE[start:PAGE.index("</form>", start)]
    assert '<select name="scene"><option value="">全部场景</option></select>' in form
    assert '<input name="scene"' not in form   # 修前是自由文本框
    start = PAGE.index("const fillTextScenes = async () => {")
    fill = PAGE[start:PAGE.index("\n  };\n", start)]
    assert 'api("/api/consents/texts?active_only=false")' in fill
    assert '`<option value="${esc(code)}">${esc(name)}</option>`' in fill
    assert "await fillTextScenes(); await drawTexts(\"\", false);" in PAGE
