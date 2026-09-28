"""写请求的页内模态框逐页改成「框自己提交、失败留框」（P2-607 棘轮，只减不增）。

`spdModal` 原先的约定是「点确定就关框、把填的值交回调用方」，调用方再发请求：请求被拒（写超了、日期写错、必填
漏了），报错落在页面的消息行，框早关了——多行的服务记录、处置意见、会诊结论写了一大段，一个 422 全部重填。
现在 `spdModal(title, fields, {submit})` 由框自己提交：失败留框、报错写在框里、填的都在；成功才关（端到端
`_spd_modal_rejected` 驱动这一种）。167 处调用点逐页迁移，从多行文本多的页起——妇幼页（5 张）第一批，慢专病任务中心（4 张）第二批，逐级转诊（3 张）第三批，智能随访（3 张）第四批，统筹调度中枢（3 张）第五批。

本文件：带多行文本、却还没给 `submit` 的模态框登记在 `KNOWN`（文件、所在函数、标题），**只减不增**：
新写一张这样的框即红；迁过的必须从名单划掉（不划也红）。只数带 `type: "textarea"` 的——只有单行字段、选择题的
框，丢了重填的代价小，不在这道棘轮里。确认框（没有字段）、只读展示的框不在其列。
"""
import re
from pathlib import Path

from jssrc import strip_comments
from test_frontend_api_calls_resolve import _call_args

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"

#: 带多行文本、还是「点确定就关框」的模态框（2026-09-27 量：妇幼页迁走 5 张后余 51，任务中心 4 张再迁走后余 47；2026-09-28 转诊 3 张迁走后余 44，随访 3 张再迁走后余 41，统筹调度 3 张再迁走后余 38）。只减不增。
KNOWN: set[tuple[str, str, str]] = {
    ('core.js', 'renderConsultations', '"出具会诊意见"'),
    ('core.js', 'renderContracts', '"记录履约"'),
    ('core.js', 'renderExams', '"可互认：30 天内已有同项目报告"'),
    ('core.js', 'renderExams', '"修订报告（改前值会连同理由留痕）"'),
    ('core.js', 'renderRx', '`处方点评（处方 ${rxcomment}）`'),
    ('core.js', 'renderRx', 'approve === "1" ? `审方通过（处方 ${id}）` : `审方驳回（处方 ${id}）`'),
    ('core.js', 'renderPharmacy', '`召回批次 ${batch ? batch.batch_no : recall}`'),
    ('core.js', 'renderChronic', '`编辑病种 ${t ? t.code : typeedit}`'),
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
    ('pages-mgmt.js', 'renderSurgery', '"术中记录"'),
    ('pages-mgmt.js', 'renderFollowups', '"完成随访"'),
    ('pages-mgmt.js', 'renderMaterials', '`到货验收：${p ? p.item_name : d.receive}`'),
    ('pages-mgmt.js', 'renderWorkflows', '"推进流程"'),
    ('pages-mgmt.js', 'renderWorkflows', '"终止流程"'),
    ('pages-mgmt.js', 'renderOutpatientDocs', '`编辑模板 ${t ? t.version : tpledit}`'),
    ('pages-mgmt.js', 'renderDiseasePrograms', '`编辑专病 ${prog ? prog.code : dpedit}`'),
    ('pages-mgmt.js', 'renderDiseasePrograms', '"记录路径节点"'),
    ('pages-public.js', 'renderCerts', '`体检 ${chkreview} 总检`'),
    ('pages-public.js', 'renderEsb', '`编辑流程 ${f ? f.code : esbflowedit}`'),
    ('pages-public.js', 'drawEduGaps', '"录考核（60 分及格；同一学员重录即更新成绩）"'),
    ('pages-public.js', 'drawImprovementTasks', '"登记整改进展"'),
    ('pages-public.js', 'drawImprovementTasks', 'impok ? "确认关闭整改任务" : "退回整改"'),
    ('pages-public.js', 'drawHomeVisits', '"完成上门服务"'),
    ('pages-spd.js', 'renderSpdAdmin', '"编辑病种（纳入 / 排除规则用该行的「改规则」，保存即升一版）"'),
    ('pages-spd.js', 'renderSpdPatients', '"服务项目扣减登记"'),
    ('pages-spd.js', 'renderSpdMember', '"办结干预"'),
    ('pages-spd.js', 'renderSpdMember', '"处置异常上报"'),
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
