"""桌面「病程记录」的类型下拉：页面不动下拉就送「首次病程」（P2-1306，第三十八批扫描 AB3-8）。

桌面那张表单的下拉按 `NOTE_TYPES`（pages-public.js）的键序生成、没有预选，首项是 first；formJson 每次都送下拉的当前值，
新入院患者先写的那一条（例如抢救记录）不动下拉就落成首次病程——真正的首次病程随后 409「首次病程记录已存在」，而住院文书
没有更正入口，错的那条改不回来。已有首次病程时不动下拉会 409、能察觉，新入院的第一条察觉不到。医生移动端查房那张下拉的
首项本就是日常病程；护理级别同形的下拉 P2-988 已改成预选。

修法：桌面病程表单预选日常病程（`PROGRESS_NOTE_DEFAULT`），与移动端一致；`NOTE_TYPES` 的键序不动（清单按类型取名也用
这张表）。首次病程漏写由文书完整性自查报出、还能补，误写成首次病程不可逆。端到端档经页面不动下拉写一条、按接口读回。
"""
import re
from pathlib import Path

from app.routers.clinical_docs import NOTE_TYPES

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


def _default() -> str:
    public = (STATIC / "pages-public.js").read_text(encoding="utf-8")
    return re.search(r'const PROGRESS_NOTE_DEFAULT = "(\w+)";', public).group(1)


def test_桌面病程表单预选日常病程():
    assert _default() == "daily" and "daily" in NOTE_TYPES
    mgmt = (STATIC / "pages-mgmt.js").read_text(encoding="utf-8")
    form = mgmt[mgmt.index('<form class="inline" id="note-form">'):]
    form = form[:form.index("</form>")]
    select = form[form.index('<select name="note_type">'):form.index("</select>")]
    assert 'k === PROGRESS_NOTE_DEFAULT ? " selected" : ""' in select   # 修前没有预选，首项 first


def test_两端缺省同一个类型_移动端首项即日常病程():
    """移动端查房的下拉没有预选、按书写顺序取首项：桌面预选的与它是同一个，两端不动下拉写下的是同一种病程。"""
    html = (STATIC / "m" / "doctor.html").read_text(encoding="utf-8")
    select = html[html.index('<select id="round-note-type">'):]
    select = select[:select.index("</select>")]
    assert re.findall(r'<option value="(\w+)"', select)[0] == _default()
