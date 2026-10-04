"""体检页把公卫录的汇总小结叫「体检结论」「结论」，同页说明又写「结论全文不在清单里」（P2-1405，第四十一批扫描 AE3-6）。

`summary` 是公卫登记时录的汇总小结——打印件上就叫「汇总小结」（`printing.print_checkup_report`）；真正的结论是医师总检
写的「总检结论」。修前登记框的占位写「体检结论」，清单那一列叫「结论」、显示的却是 summary，清单下方的说明又说「结论全文
不在清单里」：公卫录「各项正常」、医师总检写「空腹血糖 7.8 偏高…」的那一行，清单读作「结论：各项正常｜正常｜已总检」。
修后占位与列名叫「汇总小结」，说明里的结论一律写「总检结论」，与打印件同一个叫法。
"""
from pathlib import Path

from jssrc import strip_comments

APP = Path(__file__).resolve().parents[1] / "app"
#: 去掉注释再比：注释里引着修前的叫法（为什么改），页面上显示的才是要钉的
PAGE = strip_comments((APP / "static" / "pages-public.js").read_text(encoding="utf-8"))
PRINTING = (APP / "routers" / "printing.py").read_text(encoding="utf-8")


def _render() -> str:
    start = PAGE.index("async function renderCerts()")
    return PAGE[start:PAGE.index("\nasync function ", start)]


def test_登记框占位与清单列名把summary叫汇总小结():
    body = _render()
    assert '<input name="summary" placeholder="汇总小结"' in body
    assert "体检结论" not in body                                                    # 修前登记框占位「体检结论」
    assert 'table(["ID", "患者", "套餐", "日期", "汇总小结", "异常", "总检", "操作"], checkups' in body   # 修前列名「结论」


def test_清单说明里的结论一律写总检结论():
    body = _render()
    desc = body[body.index('<p class="desc">总检限医师'):]
    desc = desc[:desc.index("</p>")]
    # 修前「复核改结论」「结论全文不在清单里」「之后要看结论」三处光写「结论」
    assert desc.count("结论") == desc.count("总检结论") == 4, desc
    assert "总检结论全文不在清单里" in desc


def test_与打印件同一个叫法():
    assert "<h3>汇总小结</h3>" in PRINTING and "<h3>总检结论</h3>" in PRINTING
