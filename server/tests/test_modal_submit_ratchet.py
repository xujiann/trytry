"""写请求的页内模态框逐页改成「框自己提交、失败留框」（P2-607 棘轮，只减不增）。

`spdModal` 原先的约定是「点确定就关框、把填的值交回调用方」，调用方再发请求：请求被拒（写超了、日期写错、必填
漏了），报错落在页面的消息行，框早关了——多行的服务记录、处置意见、会诊结论写了一大段，一个 422 全部重填。
现在 `spdModal(title, fields, {submit})` 由框自己提交：失败留框、报错写在框里、填的都在；成功才关（端到端
`_spd_modal_rejected` 驱动这一种）。167 处调用点逐页迁移，从多行文本多的页起——妇幼页（5 张）第一批，慢专病任务中心（4 张）第二批，逐级转诊（3 张）第三批，智能随访（3 张）第四批，统筹调度中枢（3 张）第五批，慢专病页余下的 4 张（运行中枢、筛查建档、成员端）第六批，core.js 的 8 张（会诊、履约、检查互认与修订、处方点评与审方、批次召回、慢病病种）第七批，pages-mgmt.js 的 8 张（术中记录、完成随访、到货验收、推进 / 终止流程、同意书模板、专病目录与路径节点）第八批，pages-public.js 的 6 张（体检总检、ESB 流程、录考核、整改进展与确认退回、上门服务）第九批。

本文件：带多行文本、却还没给 `submit` 的模态框登记在 `KNOWN`（文件、所在函数、标题），**只减不增**：
新写一张这样的框即红；迁过的必须从名单划掉（不划也红）。只数带 `type: "textarea"` 的——只有单行字段、选择题的
框，丢了重填的代价小，不在这道棘轮里。确认框（没有字段）、只读展示的框不在其列。
"""
import re
from pathlib import Path

from jssrc import strip_comments
from test_frontend_api_calls_resolve import _call_args

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"

#: 带多行文本、还是「点确定就关框」的模态框（2026-09-27 量：妇幼页迁走 5 张后余 51，任务中心 4 张再迁走后余 47；2026-09-28 转诊 3 张迁走后余 44，随访 3 张再迁走后余 41，统筹调度 3 张再迁走后余 38，慢专病页余下 4 张迁走后余 34，core.js 8 张再迁走后余 26，pages-mgmt.js 8 张迁走后余 18，pages-public.js 6 张再迁走后余 12）。只减不增。
KNOWN: set[tuple[str, str, str]] = {
    ('pages-clinical.js', 'renderConsents', 'approve ? "通过申请" : "拒绝申请"'),
    ('pages-clinical.js', 'renderTelemedicine', '`回复咨询 ${reply}`'),
    ('pages-clinical.js', 'renderInsurance', 'approve ? "批准双通道申报" : "驳回双通道申报"'),
    ('pages-clinical.js', 'renderEducation', 'approve ? "排期审核" : "驳回直播申请"'),
    ('pages-clinical.js', 'renderEducation', '"直播评价（一人一场一条，再评即覆盖）"'),
    ('pages-clinical.js', 'renderVaccineSupply', '"超温处置"'),
    ('pages-clinical.js', 'renderResources', '`编辑资源 ${r ? r.code : d.rsedit}`'),
    ('pages-clinical.js', 'renderHrFinance', '"登记人员变动"'),
    ('pages-clinical.js', 'renderHrFinance', '`物资出入库：${asset ? asset.name : d.assetmv}`'),
    ('pages-clinical.js', 'renderCritical', '"处置反馈"'),
    ('pages-clinical.js', 'renderInpatient', '`病案首页（住院 ${d.summary}）`'),
    ('pages-clinical.js', 'renderQuality', 'rectify ? "登记整改措施" : "不良事件审核"'),
}


def textarea_modals_without_submit(sources: dict[str, str] | None = None) -> set[tuple[str, str, str]]:
    """`(文件, 所在函数, 标题表达式)`：带 `type: "textarea"` 字段、第三个实参里没有 `submit:` 的 `spdModal(` 调用。"""
    if sources is None:
        sources = {p.name: p.read_text(encoding="utf-8") for p in sorted(STATIC.glob("*.js"))}
    found = set()
    for name, raw in sources.items():
        src = strip_comments(raw)
        funcs = [(m.start(), m.group(1)) for m in re.finditer(r"^(?:async )?function (\w+)\s*\(", src, re.M)]
        for m in re.finditer(r"(?<!function )\bspdModal\(", src):
            args = _call_args(src, m.end())
            fields = args[1] if len(args) > 1 else ""
            opts = args[2] if len(args) > 2 else ""
            if 'type: "textarea"' not in fields or re.search(r"\bsubmit\s*:", opts):
                continue
            owner = [fn for start, fn in funcs if start < m.start()]
            found.add((name, owner[-1] if owner else "?", " ".join(args[0].split())))
    return found


def test_没有新写的点确定就关框的多行文本框():
    new = textarea_modals_without_submit() - KNOWN
    assert not new, f"带多行文本的模态框请给 submit（框自己提交、失败留框，P2-607）：{sorted(new)}"


def test_迁过的从名单划掉():
    gone = KNOWN - textarea_modals_without_submit()
    assert not gone, f"这几张框已经迁成框内提交（或改了标题 / 挪了地方），从 KNOWN 划掉：{sorted(gone)}"


def test_判据自证_认得出与放得过():
    src = {"x.js": """
async function renderA() {
  const v = await spdModal("甲", [{ name: "note", label: "备注", type: "textarea" }]);
  const w = await spdModal(`乙 ${id}`, [{ name: "note", type: "textarea" }], { submit: (v) => api("/x") });
  const u = await spdModal("丙", [{ name: "n", label: "数量", type: "number" }]);
}
function renderB() {
  // spdModal("注释里的", [{ type: "textarea" }])
  return spdModal(ok ? "丁" : "戊", [
    { name: "reason", type: "textarea" },
  ], { intro: "说明" });
}
"""}
    assert textarea_modals_without_submit(src) == {("x.js", "renderA", '"甲"'), ("x.js", "renderB", 'ok ? "丁" : "戊"')}


def test_妇幼页的多行文本框都已迁移():
    maternal = {k for k in KNOWN | textarea_modals_without_submit() if k[1] == "renderMaternal"}
    assert maternal == set()   # 第一批：妇幼页 5 张（访视、分娩、儿童访视、新筛、标高危儿）
