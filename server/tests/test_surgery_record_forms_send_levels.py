"""术中记录的两个表单都录麻醉方式与切口等级，缺省带出申请时填的（P2-179）。

移动端术中记录原先不送这两项，后端缺省全麻、II 类——医生在手机上记的每一台都记成全麻 II 类切口，手术量统计的
切口 / 麻醉构成（取的正是术中记录的这两列）跟着失真。管理端表单有这两项，但麻醉恒缺省第一项、切口恒 II 类，
不看申请时填的。端到端档按接口读回核对；这里在单元档钉住两个表单的形状。
"""
import os

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")


def _read(*parts):
    with open(os.path.join(STATIC, *parts), encoding="utf-8") as fh:
        return fh.read()


def test_移动端术中记录表单录两项并送出_缺省取申请的():
    source = _read("m", "doctor.js")
    assert 'data-anesthesia="${esc(r.anesthesia_type)}" data-incision="${esc(r.incision_level)}"' in source
    start = source.index('form.className = "surg-record-form"')
    form = source[start:source.index('form.querySelector("[data-cancel]")', start)]
    for field in ("anesthesia_type", "incision_level"):
        assert f'<select name="{field}">' in form, field
        assert f"{field}: f.{field}.value" in form, field


def test_移动端术中记录录并发症并送出():
    """P2-861（第二十三批「页面表单提交的字段与取值 vs 后端请求模型」扫描 Y1-10）：移动端原先不录并发症，手机上记的每一台
    都进「手术并发症发生率」的分母、进不了分子（术中所见写了「术后切口感染」也一样）；管理端同一张记录早有这一项。"""
    source = _read("m", "doctor.js")
    start = source.index('form.className = "surg-record-form"')
    form = source[start:source.index('form.querySelector("[data-cancel]")', start)]
    assert '<input name="complications"' in form and "complications: f.complications.value.trim()," in form


def test_管理端术中记录的两项缺省取申请的():
    source = _read("pages-mgmt.js")
    assert 'value: req ? req.anesthesia_type : "general"' in source
    assert 'value: req ? req.incision_level : "II"' in source
