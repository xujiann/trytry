"""查询「接住了」错误却不清结果区：计费明细、授权记录、患者检索、证书筛选、慢专病看板与日历，查失败时上一位的结果照旧挂着
（P2-1010，第二十九批「页面状态残留与失败处理」扫描 E2-6）。

P2-378 把一批查询改成「先清空、查不到把原因写出来」，还有几处只做了后一半：
- 收费「计费明细」：原因写进上方「收费项目目录」面板的消息行，明细区照旧是上一位的明细与合计（真浏览器实测：收费员查乙 403，
  明细区仍是甲的「雾化吸入 35×4 未结清」）；
- 调阅授权：换号查不到（404），上一位的授权清单连同「撤销」按钮还挂着，按钮上是上一位的患者号——点撤销撤的是上一位；
- 患者检索：原因写进上方建档面板，列表照旧；证书筛选、慢专病在管 / 任务 / 随访看板筛选：只写原因，列表照旧；
- 慢专病健康日历、随访前置资料：换号 / 换一条取不到，挂着的是上一位的随访、就诊、住院与电话。

修法：查之前先清空结果区；计费明细与患者检索的原因写在本块。
"""
from pathlib import Path

from jssrc import strip_comments

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


def _src(name):
    return strip_comments((STATIC / name).read_text(encoding="utf-8"))


def _block(name, marker, end="\n  };\n"):
    src = _src(name)
    start = src.index(marker)
    return src[start:src.index(end, start)]


def _clears_before(body, clear, call):
    assert clear in body, f"没有先清空：{clear}"
    assert body.index(clear) < body.index(call), f"清空要在发请求之前：{clear}"


def test_计费明细查失败清空明细_原因写在本面板():
    body = _block("pages-clinical.js", '$("#bd-query").onsubmit')
    _clears_before(body, '$("#bd-list").innerHTML = "";', "await api(")
    assert '"#bill-msg"' not in body, "原因还写在上方收费项目目录面板里"
    assert "catch (err) { fail(err.message); }" in body


def test_授权记录换号先清空_撤销按钮不留上一位的():
    body = _block("core.js", "const drawAuths = async")
    _clears_before(body, '$("#auth-table").innerHTML = "";', "await api(")


def test_患者检索查失败清空列表_原因写在检索这一块():
    body = _block("core.js", '$("#patient-search").onsubmit')
    _clears_before(body, '$("#patient-table").innerHTML = "";', "await draw(")
    catch = body[body.index("catch (err)"):]
    assert '$("#patient-table").innerHTML' in catch and '"#patient-msg"' not in catch


def test_证书筛选查失败清空列表():
    body = _block("pages-public.js", '$("#cert-filter").onsubmit')
    _clears_before(body, '$("#cert-table").innerHTML = "";', "await draw(")


def test_慢专病看板筛选查失败清空列表():
    for form, lst, fn in (("#spd-enroll-filter", "#spd-enroll-list", "drawEnrollments"),
                          ("#spd-task-filter", "#spd-task-list", "drawTasks"),
                          ("#spd-fu-filter", "#spd-fu-list", "drawRecords")):
        body = _block("pages-spd.js", f'$("{form}").onsubmit')
        _clears_before(body, f'$("{lst}").innerHTML = "";', f"await {fn}(")


def test_健康日历与随访前置资料换人先清空():
    body = _block("pages-spd.js", '$("#spd-cal-form").onsubmit')
    _clears_before(body, '$("#spd-cal-box").innerHTML = "";', "await api(")
    src = _src("pages-spd.js")
    ctx = src[src.index("if (ctx) {"):]
    ctx = ctx[:ctx.index("return;")]
    _clears_before(ctx, '$("#spd-fu-detail").innerHTML = "";', "await api(")
