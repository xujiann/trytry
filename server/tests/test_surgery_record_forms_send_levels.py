"""术中记录的两个表单都录麻醉方式与切口等级，缺省带出申请时填的（P2-179）。

移动端术中记录原先不送这两项，后端缺省全麻、II 类——医生在手机上记的每一台都记成全麻 II 类切口，手术量统计的
切口 / 麻醉构成（取的正是术中记录的这两列）跟着失真。管理端表单有这两项，但麻醉恒缺省第一项、切口恒 II 类，
不看申请时填的。端到端档按接口读回核对；这里在单元档钉住两个表单的形状。
P2-1116 起两个表单还录手术起止时刻（缺省带出排班日期与时段），见最后一条。
"""
import os

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")


def _read(*parts):
    with open(os.path.join(STATIC, *parts), encoding="utf-8") as fh:
        return fh.read()


def _mobile_record_form(source):
    """移动端术中记录那一段。P2-1093 起经 cardForm 插表单（报错写进这张卡），从 cardForm 的调用切到提交成功后重载列表为止"""
    start = source.index('cardForm(e.target.closest(".m-card"), "surg-record-form"')
    return source[start:source.index("await loadSurgery();", start)]


def test_移动端术中记录表单录两项并送出_缺省取申请的():
    source = _read("m", "doctor.js")
    assert 'data-anesthesia="${esc(r.anesthesia_type)}" data-incision="${esc(r.incision_level)}"' in source
    form = _mobile_record_form(source)
    for field in ("anesthesia_type", "incision_level"):
        assert f'<select name="{field}">' in form, field
        assert f"{field}: f.{field}.value" in form, field


def test_移动端术中记录录并发症并送出():
    """P2-861（第二十三批「页面表单提交的字段与取值 vs 后端请求模型」扫描 Y1-10）：移动端原先不录并发症，手机上记的每一台
    都进「手术并发症发生率」的分母、进不了分子（术中所见写了「术后切口感染」也一样）；管理端同一张记录早有这一项。"""
    source = _read("m", "doctor.js")
    form = _mobile_record_form(source)
    assert '<input name="complications"' in form and "complications: f.complications.value.trim()," in form


def test_管理端术中记录的两项缺省取申请的():
    source = _read("pages-mgmt.js")
    assert 'value: req ? req.anesthesia_type : "general"' in source
    assert 'value: req ? req.incision_level : "II"' in source


def _mgmt_record_modal(source):
    """管理端术中记录那一段：框自己提交（P2-607），从取排班起、到框关掉后的 `if (!ok) return;` 为止。"""
    start = source.index("const slot = schedules.find((s) => s.request_id === Number(d.record));")
    return source[start:source.index("if (!ok) return;", start)]


def test_两端术中记录表单录手术起止并送出_缺省带出排班日期与时段():
    """P2-1116（第三十二批「跨对象的业务时间先后」扫描 B2-2）：两端表单原先都不送 `start_at` / `end_at`，做手术那天
    （术后随访起算、手术质量指标归月，`surgery.operation_day`）恒取排班日——顺延、提前的手术都跟着原排班日走，管理端
    「查看记录」的「起止」一行恒空。现在两端都录，缺省带出这台的排班日期与时段、可改；排班表只列今天及以后，更早的排班
    带不出来就留空（后端按排班日）。端到端档经页面各走一遍、按接口读回。"""
    mobile = _read("m", "doctor.js")
    # 排班日期与时段跟着「填写术中记录」按钮走：待填清单按申请号找到这台的排班
    assert "const slots = Object.fromEntries(schedules.map((s) => [s.request_id, s]));" in mobile
    assert "const start = slot ? `${slot.scheduled_date} ${slot.start_time}` : \"\";" in mobile
    assert "const end = slot ? `${slot.scheduled_date} ${slot.end_time}` : \"\";" in mobile
    assert 'data-start="${esc(start)}" data-end="${esc(end)}"' in mobile
    form = _mobile_record_form(mobile)
    for field, key in (("start_at", "start"), ("end_at", "end")):
        assert f'<input name="{field}"' in form and f'value="${{esc(e.target.dataset.{key} || "")}}"' in form, field
        assert f"{field}: f.{field}.value.trim()," in form, field

    modal = _mgmt_record_modal(_read("pages-mgmt.js"))
    for field, key in (("start_at", "start_time"), ("end_at", "end_time")):
        assert f'{{ name: "{field}", label: ' in modal, field   # spdModal 把每个字段按名字交给 submit
        assert f"value: slot ? `${{slot.scheduled_date}} ${{slot.{key}}}` : \"\"" in modal, field
    assert 'submit: (v) => api(`/api/surgery/requests/${d.record}/record`, { method: "POST", body: JSON.stringify(v) })' in modal
