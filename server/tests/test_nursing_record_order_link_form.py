"""住院护理记录表单补「关联医嘱」（P2-863，第二十三批「页面表单提交的字段与取值 vs 后端请求模型」扫描 Y1-12）。

`NursingIn.inpatient_order_id` 早就收（P1-24a：本条护理记录若由执行某条医嘱产生，传该医嘱 id，同一次住院才收），医嘱执行
视图据它数「关联护理记录 N 条」、执行弹窗的说明也这么写；护理记录表单却没有这一项，按界面用法这个数恒为 0。修后表单
列本次住院在用的医嘱供选，选了按数送。
"""
from pathlib import Path

PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-mgmt.js").read_text(encoding="utf-8")


def test_护理记录表单有关联医嘱_按数送():
    start = PAGE.index('<form class="inline" id="nursing-form">')
    form = PAGE[start:PAGE.index("</form>", start)]
    assert '<select name="inpatient_order_id">' in form and "activeOrders.map((o) =>" in form   # 修前没有
    assert "api(`/api/inpatient/orders?admission_id=${current}&status=active`)" in PAGE
    assert 'formJson(e.target, ["inpatient_order_id"])' in PAGE
