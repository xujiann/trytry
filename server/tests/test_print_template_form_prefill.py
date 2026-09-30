"""打印模板维护表单不带出现值：只改页脚，就把抬头清空、把关掉的验真二维码重新打开（P2-993，第二十八批「缺省值」扫描 F4-7）。

表单的抬头、页脚框空白，`show_qr` 恒勾选，选单据类型不回填；保存时三项整条送出，`PUT /api/print/templates` 整条覆盖（upsert）。
处方笺已配好抬头「某县医共体总院」、页脚「本处方当日有效」并关掉二维码，只想把页脚改成「3 日内有效」，结果抬头变空、二维码
重新打开。P2-313 / P2-960 / P2-968 是同一条规矩：编辑框按现值预填。

修法：页面渲染时与每次切换单据类型时，把这一类的现值（清单接口本来就给）回填进三项。
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-public.js").read_text(encoding="utf-8")


def _handler() -> str:
    start = PAGE.index("async function renderPrintTemplates()")
    return PAGE[start:PAGE.index("\n}\n", start)]


def test_切换单据类型回填现值_渲染时先填一次():
    body = _handler()
    assert "tplForm.doc_type.onchange = fillTemplate;" in body and "fillTemplate();" in body
    for field in ("tplForm.header_org_name.value = t.header_org_name", "tplForm.footer_note.value = t.footer_note",
                  "tplForm.show_qr.checked = !!t.show_qr"):
        assert field in body, field


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 可执行这段前端函数")
def test_跑一遍回填_只改页脚时抬头与二维码保持原值():
    body = _handler()
    start = body.index("const fillTemplate = () => {")
    fill = body[start:body.index("};", start) + 2]
    script = (
        "const templates = JSON.parse(process.argv[1]);"
        "const tplForm = {doc_type: {value: 'prescription'}, header_org_name: {value: ''}, footer_note: {value: ''},"
        " show_qr: {checked: true}};"
        + fill +
        "fillTemplate();"
        "console.log(JSON.stringify([tplForm.header_org_name.value, tplForm.footer_note.value, tplForm.show_qr.checked]));"
    )
    templates = [{"doc_type": "prescription", "header_org_name": "某县医共体总院", "footer_note": "本处方当日有效",
                  "show_qr": False}]
    out = subprocess.run(["node", "-e", script, json.dumps(templates, ensure_ascii=False)], capture_output=True,
                         text=True, check=True, timeout=60).stdout
    assert json.loads(out) == ["某县医共体总院", "本处方当日有效", False]   # 修前 ['', '', True]
