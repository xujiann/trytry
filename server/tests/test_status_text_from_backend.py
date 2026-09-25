"""工作人员端把英文状态码原样显示：状态文案取自后端（P2-72，§13 功能完整第 7 项）。

2026-09-25 按形状全仓扫出 14 处：页面单元格里直接 `${esc(x.status)}`，或在三元里只译一两个值、其余
`'<span class="tag">' + esc(x.status)` 兜底——药剂科看到的制剂批次状态是 `produced`，急救调度看到
`en_route`，随访看板上失访的记录显示 `unreachable`，健康日历里的随访 / 复诊 / 任务一个都不译。
特病申报队列那一处还是裸插值 `${a.status}`（值只由服务端写，眼下不可利用，但违反 §8「innerHTML 插值
不得漏转义」）。

同日把字段从状态扩到场景（`scene`）与异常分级（`abnormal_level`）又扫出 11 处（P2-73）：随访各表显示 `inpatient`，
居民自助随访后手机上弹「系统判定为high异常」，同意书管理页显示 `chronic_enroll` / `self`。

报错文案同一个病（P2-74 ①）：平台侧 47 处 409 的 `detail=f"当前状态 {x.status} 不可…"`，窗口人员看到的是
「当前状态 approved 不可审批」「基金池状态为 settled，不可再预付」；改成各路由文案表的 `NAMES.get(code, code)`
（与 billing 早有的 `PAYMENT_STATUS.get(...)` 同一写法），并立报错文案这一侧的零基线闸门。

修法：出参补后端给的 `status_name`（与既有 `MAP.get(code, code)` 惯例一致：表外的值原样回显，
「后端加了新状态、表还没跟上」的现场看得见），页面改显示它；同一文件里已有同一套状态文案表的
（慢专病复诊、任务）直接复用那张表，不另起一份措辞。

本文件：
- 前端闸门：页面文字里不许出现原样的状态码插值（派生零基线 + 按设计名单只减不增，每条写理由）；
- 报错文案闸门：`HTTPException` 的 detail f-string 里不许直接插 `.status` / `.xxx_status`（派生零基线）；
- 文案表对照列注释：每张新文案表的键与模型列注释里列出的状态码一一对应——加了新状态没补文案，
  这里先红（否则页面上又是一个英文码）；
- 端点回归：没有契约网钉着的三处（急救事件、特病申报、报告修订回执）走一遍状态流转。
"""
import ast
import inspect
import re
import sys
from collections import Counter
from pathlib import Path

import pytest

from test_frontend_escape_guard import _strip_comments  # 同一份注释剥离：块注释换等量换行，行号不错位

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"

#: 零基线（`scripts/dump_gate_status.py` 把它列进闸门现状）。
#: 2026-09-25 实测（修前 377c0fe）：页面文字里原样的状态码插值 18 处，按设计 3 → 其余 15 处已改显示后端文案
#: （初版判据漏了带兜底的写法，量出 17；补上后多认出凭证核验页那一处）。
#: 同日字段扩到场景 / 异常分级（P2-73，修前 27328ed）：场景 8 处 + 异常分级 3 处 → 0，按设计名单不变。
BASELINE = 0

#: 按设计原样显示的（`(文件, 插值)` → 理由）。**只减不增**；每条只抵一处——同一文件再添一句同样的插值照样红。
BY_DESIGN = {
    ("pages-mgmt.js", "esc(i.raw_status)"):
        "统一在办事项的「原生状态」一栏：刻意并排给出各业务表自己的状态码供对照排查，同一行「统一状态」才是给人读的文案",
    ("pages-mgmt.js", "${r.status}"):
        "运行监控页错误样本的 HTTP 状态码（数字），不是业务状态",
    ("pages-public.js", "${x.status}"):
        "ESB 流程执行记录的步骤结果快照（`步骤.类型=结果`），给对接工程师排障的技术串，整串已 esc()",
}

#: 判的字段：状态（`status` / `*_status`）、场景（`scene`）、异常分级（`abnormal_level`）——后两个是 P2-73 补进来的，
#: 都是后端的封闭码表、都曾原样显示（随访看板的 inpatient、居民手机上的「系统判定为high异常」）。
_FIELD = r"([A-Za-z_$][\w$]*)\.(?:(?:[a-z_]*_)?status|scene|abnormal_level)"
#: 把状态码原样放进页面的写法：`esc(x.status)`（插值或字符串拼接里）、裸插值 `${x.status}`、两者带兜底
#: （`${x.status || "未知"}` / `esc(x.status ?? "")`——有值时显示的照样是原码）、裸拼接 `+ x.status +`。
#: `status_name` / `status_code` 不命中（`status` 后面必须紧跟右括号、右花括号、兜底运算符或加号）。
#: 兜底那两种是后补的（初版漏了，凭证核验页「失效（void）」就是这个形状）。
RAW_STATUS = re.compile(
    rf"(?:esc\(|\$\{{)\s*{_FIELD}\s*(?:\)|\}}|\|\||\?\?)"
    rf"|\+\s*{_FIELD}\s*\+"
)
#: 同一行内双引号包着的属性值（`data-status="${esc(t.status)}"`）：取值进属性、给脚本回读，不是页面文字
ATTR_VALUE = re.compile(r'[\w-]+="[^"\n]*"')
#: fetch 的 Response：`resp.status` 是 HTTP 状态码
HTTP_RESPONSE_NAMES = {"resp"}


def raw_status_displays(files=None) -> list[tuple[str, str, int]]:
    """`(文件, 插值, 行号)`：页面文字里原样的状态码插值。"""
    found = []
    for path in files if files is not None else sorted(STATIC.rglob("*.js")):
        label = path.relative_to(STATIC).as_posix() if path.is_relative_to(STATIC) else path.name
        for lineno, line in enumerate(_strip_comments(path.read_text(encoding="utf-8")).splitlines(), 1):
            for m in RAW_STATUS.finditer(ATTR_VALUE.sub('""', line)):
                owner = next(g for g in m.groups() if g)
                if owner not in HTTP_RESPONSE_NAMES:
                    found.append((label, m.group(0), lineno))
    return found


def _minus_by_design(found):
    budget = Counter(BY_DESIGN.keys())
    rest = []
    for label, snippet, lineno in found:
        if budget[(label, snippet)] > 0:
            budget[(label, snippet)] -= 1
        else:
            rest.append(f"{label}:{lineno}: {snippet}")
    return rest


# ================================================================ 前端闸门
def test_页面上不原样显示状态码():
    bad = _minus_by_design(raw_status_displays())
    assert len(bad) <= BASELINE, (
        "这些地方把后端的状态码原样显示给用户（§13 第 7 项：状态文案取自后端）：\n  " + "\n  ".join(bad)
        + "\n\n出参补 `status_name`（后端文案表 `MAP.get(code, code)`）并显示它；同一文件已有同一套状态的文案表就复用；"
        "确实该显示原码的（技术串、HTTP 状态码），进 BY_DESIGN 写明理由。"
    )


def test_按设计名单只减不增():
    present = Counter((label, snippet) for label, snippet, _ in raw_status_displays())
    stale = sorted(f"{label}: {snippet}" for label, snippet in BY_DESIGN if present[(label, snippet)] == 0)
    assert stale == [], "这些已经改掉或挪走了，请从 BY_DESIGN 划掉：\n  " + "\n  ".join(stale)


def test_判据自证_三种写法都认得出_写对的放过(tmp_path):
    probe = tmp_path / "probe.js"
    probe.write_text(
        # 应命中的三种
        "a = `<td>${esc(b.status)}</td>`;\n"
        "c = `<td>${d.critical_status}</td>`;\n"
        "e = '<span class=\"tag\">' + esc(f.status) + '</span>' + g.status + '';\n"
        "s = `<b>${esc(t.status || \"—\")}</b><b>${u.credential_status ?? \"未知\"}</b>`;\n"
        "v = `<td>${esc(w.scene)}</td><td>${esc(x.scene_name)}</td>判定为${y.abnormal_level}异常`;\n"
        # 应放过的：后端文案、查表组件、属性值、HTTP 状态码、别的字段、注释
        "h = `<td>${esc(i.status_name)}</td><td>${statusTag(MAP, j.status)}</td>`;\n"
        "k = `<button data-status=\"${esc(l.status)}\">改</button>`;\n"
        "m = new Error(`请求失败(${resp.status})`);\n"
        "n = `<td>${o.status_code}</td><td>${esc(p.status_text)}</td>`;\n"
        "// q = `<td>${esc(r.status)}</td>`;\n",
        encoding="utf-8",
    )
    hits = [(snippet, lineno) for _, snippet, lineno in raw_status_displays([probe])]
    assert hits == [("esc(b.status)", 1), ("${d.critical_status}", 2),
                    ("esc(f.status)", 3), ("+ g.status +", 3),
                    ("esc(t.status ||", 4), ("${u.credential_status ??", 4),
                    ("esc(w.scene)", 5), ("${y.abnormal_level}", 5)], hits


def test_判据自证_同一插值每条豁免只抵一处(tmp_path, monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "BY_DESIGN", {("probe.js", "${r.status}"): "理由"})
    probe = tmp_path / "probe.js"
    probe.write_text("a = `<td>${r.status}</td>`;\nb = `<td>${r.status}</td>`;\n", encoding="utf-8")
    assert _minus_by_design(raw_status_displays([probe])) == ["probe.js:2: ${r.status}"]


def test_判据自证_扫描面覆盖三端():
    """防空转：认不出任何状态插值时闸门会空转成绿——三端源码里状态字段的引用必须扫得到。"""
    files = {p.relative_to(STATIC).as_posix() for p in STATIC.rglob("*.js")}
    assert {"core.js", "pages-spd.js", "m/m.js", "m/doctor.js"} <= files
    total = sum(p.read_text(encoding="utf-8").count(".status") for p in STATIC.rglob("*.js"))
    assert total >= 300, total


# ================================================================ 报错文案不直接拼状态码（P2-74 ①）
APP = STATIC.parent

#: 零基线。2026-09-25 实测（修前 90a4afe）：`HTTPException` 的 detail f-string 里直接插 `.status` / `.xxx_status` 的
#: 48 处（47 条报错、24 个路由文件；转诊那一条前后两个码）→ 0。
DETAIL_BASELINE = 0


def raw_status_in_details(sources) -> list[str]:
    """`(标签, 源码)` 里 `HTTPException(...)` 的 detail（关键字或第二个位置参数）f-string 直接插状态列的位置。

    `NAMES.get(x.status, x.status)` 不命中（插的是 Call 而不是 Attribute）；`status_code` / `status_name` 不命中。
    """
    hits = []
    for label, source in sources:
        for call in ast.walk(ast.parse(source)):
            if not isinstance(call, ast.Call) or not ast.unparse(call.func).endswith("HTTPException"):
                continue
            details = [kw.value for kw in call.keywords if kw.arg == "detail"] + call.args[1:2]
            for detail in details:
                if not isinstance(detail, ast.JoinedStr):
                    continue
                for part in detail.values:
                    if (isinstance(part, ast.FormattedValue) and isinstance(part.value, ast.Attribute)
                            and re.fullmatch(r"(?:[a-z_]*_)?status", part.value.attr)):
                        hits.append(f"{label}:{call.lineno}: {ast.unparse(part.value)}")
    return hits


def _app_sources():
    for path in sorted(APP.rglob("*.py")):
        if "__pycache__" not in path.parts:
            yield path.relative_to(APP).as_posix(), path.read_text(encoding="utf-8")


def test_报错文案不直接拼英文状态码():
    hits = raw_status_in_details(_app_sources())
    assert len(hits) == DETAIL_BASELINE, (
        "409 / 422 的报错文案里直接拼了状态码——窗口人员看到的是「当前状态 approved 不可审批」。"
        "用本路由的状态文案表 `NAMES.get(x.status, x.status)`（没有就照模型列注释建一张，并登记进下面的文案表名单）：\n"
        + "\n".join(hits)
    )


def test_判据自证_报错文案():
    probe = (
        'raise HTTPException(status_code=409, detail=f"当前状态 {a.status} 不可审批")\n'
        'raise HTTPException(409, f"危急值 {b.critical_status} 不可确认")\n'
        'raise HTTPException(status_code=409, detail=f"当前状态 {NAMES.get(c.status, c.status)} 不可审批")\n'
        'raise HTTPException(status_code=502, detail=f"对端返回 {d.status_code}")\n'
        'raise HTTPException(status_code=409, detail="当前状态不可审批")\n'
        'x = f"{e.status}"\n'
    )
    assert raw_status_in_details([("probe.py", probe)]) == ["probe.py:1: a.status", "probe.py:2: b.critical_status"]
    # 防空转：扫描面里 HTTPException 的 f-string detail 得有一大把，才说明真在扫
    total = sum(src.count('detail=f"') for _, src in _app_sources())
    assert total >= 150, total   # 2026-09-25 实测 170


# ================================================================ 文案表对照列注释
def _column_codes(model, column: str) -> set[str]:
    """模型源码里 `column: Mapped…` 上方紧挨着的注释块列出的码（`code=文案` 形状，`""` 也算一个；注释可跨行）。"""
    lines = inspect.getsource(model).splitlines()
    at = next(i for i, line in enumerate(lines) if re.match(rf"\s+{column}: Mapped", line))
    block = []
    while at > 0 and lines[at - 1].strip().startswith("#"):
        at -= 1
        block.append(lines[at])
    assert block, f"{model.__name__}.{column} 上方没有列出取值的注释"
    return {"" if code == '""' else code for code in re.findall(r'(""|[a-z][a-z_]*)=', "\n".join(block))}


def _label_tables():
    from app.models import (Admission, AdverseEvent, Appointment, ConsentRecord, Consultation, CorrectionRequest,
                            DrugShortage, EmergencyCase, EsbMessage, ExamReport, ExamRequest, FollowupTask, FundPool,
                            HighValueConsumable, HomeVisitOrder, MaterialPurchase, MedicalWaste, OnlineConsult,
                            PathologySpecimen, Prescription, Referral, SpecialDiseaseApp, SpdCenter,
                            SpdFollowupRecord, SpdFollowupRule, SpdPathTemplate, SterilizationBatch, SurgeryRequest,
                            TcmDispenseOrder, TcmPreparationBatch, TrainingEnrollment, TrainingPlan, VisitCredential,
                            Voucher, WorkflowInstance)
    from app.models import AccountSubject, ChargeItem, Course, Department, OfficialDoc, Organization, SimulationCase
    from app.models import (Encounter, SpdAssessPlan, SpdEduMaterial, SpdGroup, SpdMeasurement, SpdPathNode,
                            SpdScreening, SpdTag, SpdTeam)
    from app.spd import service
    from app.spd.routers import assess, population
    from app.routers import (accounting, admin_mgmt, appointments, billing, consents, consultations, credentials, cssd,
                             education, emergency, esb, exams, followups, fund, homevisits, insurance, materials,
                             medication, medwaste, organizations, pathology, prescriptions, quality, referrals,
                             surgery, tcm, tcm_heritage, telemedicine, workflows)
    from app.spd.routers import followup, workbench
    from app.spd.routers.config import paths, scales

    # (文案表, 模型, 列, 按设计不进表的码)
    return {
        "emergency.CASE_STATUS_NAMES": (emergency.CASE_STATUS_NAMES, EmergencyCase, "status", set()),
        "tcm.BATCH_STATUS_NAMES": (tcm.BATCH_STATUS_NAMES, TcmPreparationBatch, "status", set()),
        "education.PLAN_STATUS_NAMES": (education.PLAN_STATUS_NAMES, TrainingPlan, "status", set()),
        "education.ENROLLMENT_STATUS_NAMES":
            (education.ENROLLMENT_STATUS_NAMES, TrainingEnrollment, "status", set()),
        "insurance.SPECIAL_DISEASE_STATUS_NAMES":
            (insurance.SPECIAL_DISEASE_STATUS_NAMES, SpecialDiseaseApp, "status", set()),
        # 空串 = 非危急值，没有闭环状态，文案也是空串
        "exams.CRITICAL_STATUS_NAMES": (exams.CRITICAL_STATUS_NAMES, ExamReport, "critical_status", {""}),
        "workbench.CENTER_STATUS_NAMES": (workbench.CENTER_STATUS_NAMES, SpdCenter, "status", set()),
        "followup.FOLLOWUP_STATUS_NAMES":
            (followup.FOLLOWUP_STATUS_NAMES, SpdFollowupRecord, "status", set()),
        "followup.ADMISSION_STATUS_NAMES": (followup.ADMISSION_STATUS_NAMES, Admission, "status", set()),
        # 既有的表，本批起凭证核验回执也用它
        "credentials.STATUS_NAMES": (credentials.STATUS_NAMES, VisitCredential, "status", set()),
        # P2-73：场景 / 异常分级 / 同意方式
        "followup.FOLLOWUP_SCENE_NAMES": (followup.FOLLOWUP_SCENE_NAMES, SpdFollowupRule, "scene", set()),
        "followup.ABNORMAL_LEVEL_NAMES":
            (followup.ABNORMAL_LEVEL_NAMES, SpdFollowupRecord, "abnormal_level", set()),
        "consents.CONSENT_SCENE_NAMES": (consents.CONSENT_SCENE_NAMES, ConsentRecord, "scene", set()),
        "consents.CONSENT_METHOD_NAMES": (consents.CONSENT_METHOD_NAMES, ConsentRecord, "method", set()),
        "paths.PATH_SCENE_NAMES": (paths.PATH_SCENE_NAMES, SpdPathTemplate, "scene", set()),
        # P2-74 ①：409 报错文案用的表（esb / pathology / referrals 三张是既有的，本批起报错也用它）
        "accounting.VOUCHER_STATUS_NAMES": (accounting.VOUCHER_STATUS_NAMES, Voucher, "status", set()),
        "appointments.APPOINTMENT_STATUS_NAMES":
            (appointments.APPOINTMENT_STATUS_NAMES, Appointment, "status", set()),
        "consents.CORRECTION_STATUS_NAMES": (consents.CORRECTION_STATUS_NAMES, CorrectionRequest, "status", set()),
        "consultations.CONSULTATION_STATUS_NAMES":
            (consultations.CONSULTATION_STATUS_NAMES, Consultation, "status", set()),
        "cssd.STERILIZATION_STATUS_NAMES": (cssd.STERILIZATION_STATUS_NAMES, SterilizationBatch, "status", set()),
        "prescriptions.PRESCRIPTION_STATUS_NAMES":
            (prescriptions.PRESCRIPTION_STATUS_NAMES, Prescription, "status", set()),
        "esb.MSG_STATUS": (esb.MSG_STATUS, EsbMessage, "status", set()),
        "exams.EXAM_REQUEST_STATUS_NAMES": (exams.EXAM_REQUEST_STATUS_NAMES, ExamRequest, "status", set()),
        "followups.FOLLOWUP_TASK_STATUS_NAMES": (followups.FOLLOWUP_TASK_STATUS_NAMES, FollowupTask, "status", set()),
        "fund.POOL_STATUS_NAMES": (fund.POOL_STATUS_NAMES, FundPool, "status", set()),
        "homevisits.VISIT_ORDER_STATUS_NAMES":
            (homevisits.VISIT_ORDER_STATUS_NAMES, HomeVisitOrder, "status", set()),
        "materials.PURCHASE_STATUS_NAMES": (materials.PURCHASE_STATUS_NAMES, MaterialPurchase, "status", set()),
        "materials.CONSUMABLE_STATUS_NAMES":
            (materials.CONSUMABLE_STATUS_NAMES, HighValueConsumable, "status", set()),
        "medication.SHORTAGE_STATUS_NAMES": (medication.SHORTAGE_STATUS_NAMES, DrugShortage, "status", set()),
        "medwaste.WASTE_STATUS_NAMES": (medwaste.WASTE_STATUS_NAMES, MedicalWaste, "status", set()),
        "pathology.SPECIMEN_STATUS": (pathology.SPECIMEN_STATUS, PathologySpecimen, "status", set()),
        "quality.ADVERSE_EVENT_STATUS_NAMES": (quality.ADVERSE_EVENT_STATUS_NAMES, AdverseEvent, "status", set()),
        "referrals.STATUS_LABELS": (referrals.STATUS_LABELS, Referral, "status", set()),
        "surgery.SURGERY_STATUS_NAMES": (surgery.SURGERY_STATUS_NAMES, SurgeryRequest, "status", set()),
        "tcm.DISPENSE_ORDER_STATUS_NAMES": (tcm.DISPENSE_ORDER_STATUS_NAMES, TcmDispenseOrder, "status", set()),
        "telemedicine.CONSULT_STATUS_NAMES": (telemedicine.CONSULT_STATUS_NAMES, OnlineConsult, "status", set()),
        "workflows.INSTANCE_STATUS_NAMES": (workflows.INSTANCE_STATUS_NAMES, WorkflowInstance, "status", set()),
        # P2-74 ② 平台侧：状态之外的封闭码表字段（accounting / billing 两张是既有的，本批起清单也用它）
        "organizations.ORG_LEVEL_NAMES": (organizations.ORG_LEVEL_NAMES, Organization, "level", set()),
        "accounting.CATEGORY_NAMES": (accounting.CATEGORY_NAMES, AccountSubject, "category", set()),
        "education.COURSE_CATEGORY_NAMES": (education.COURSE_CATEGORY_NAMES, Course, "category", set()),
        "tcm_heritage.SIMULATION_CATEGORY_NAMES":
            (tcm_heritage.SIMULATION_CATEGORY_NAMES, SimulationCase, "category", set()),
        "admin_mgmt.DEPT_CATEGORY_NAMES": (admin_mgmt.DEPT_CATEGORY_NAMES, Department, "category", set()),
        "admin_mgmt.DOC_TYPE_NAMES": (admin_mgmt.DOC_TYPE_NAMES, OfficialDoc, "doc_type", set()),
        "billing.CHARGE_CATEGORY_NAMES": (billing.CHARGE_CATEGORY_NAMES, ChargeItem, "category", set()),
        "consents.CORRECTION_TYPE_NAMES":
            (consents.CORRECTION_TYPE_NAMES, CorrectionRequest, "request_type", set()),
        # P2-74 ② 慢专病侧（纳管网络树的机构层级沿用上面的 organizations.ORG_LEVEL_NAMES）
        "service.MEASUREMENT_SOURCE_NAMES": (service.MEASUREMENT_SOURCE_NAMES, SpdMeasurement, "source", set()),
        "service.MEDIA_TYPE_NAMES": (service.MEDIA_TYPE_NAMES, SpdEduMaterial, "media_type", set()),
        "scales.TAG_CATEGORY_NAMES": (scales.TAG_CATEGORY_NAMES, SpdTag, "category", set()),
        "population.SCREENING_SOURCE_NAMES": (population.SCREENING_SOURCE_NAMES, SpdScreening, "source", set()),
        "population.GROUP_SCOPE_NAMES": (population.GROUP_SCOPE_NAMES, SpdGroup, "scope", set()),
        "workbench.TEAM_LEVEL_NAMES": (workbench.TEAM_LEVEL_NAMES, SpdTeam, "level", set()),
        "paths.NODE_SERVICE_TYPE_NAMES": (paths.NODE_SERVICE_TYPE_NAMES, SpdPathNode, "service_type", set()),
        "assess.ASSESS_LEVEL_NAMES": (assess.ASSESS_LEVEL_NAMES, SpdAssessPlan, "level", set()),
        "assess.PERIOD_TYPE_NAMES": (assess.PERIOD_TYPE_NAMES, SpdAssessPlan, "period_type", set()),
        "followup.ENCOUNTER_TYPE_NAMES": (followup.ENCOUNTER_TYPE_NAMES, Encounter, "encounter_type", set()),
    }


#: 本批新建或新用上的文案表（`_label_tables` 的键）；参数化用静态名单，收集阶段不导入应用
LABEL_TABLE_NAMES = [
    "emergency.CASE_STATUS_NAMES", "tcm.BATCH_STATUS_NAMES", "education.PLAN_STATUS_NAMES",
    "education.ENROLLMENT_STATUS_NAMES", "insurance.SPECIAL_DISEASE_STATUS_NAMES", "exams.CRITICAL_STATUS_NAMES",
    "workbench.CENTER_STATUS_NAMES", "followup.FOLLOWUP_STATUS_NAMES", "followup.ADMISSION_STATUS_NAMES",
    "credentials.STATUS_NAMES",
    "followup.FOLLOWUP_SCENE_NAMES", "followup.ABNORMAL_LEVEL_NAMES", "consents.CONSENT_SCENE_NAMES",
    "consents.CONSENT_METHOD_NAMES", "paths.PATH_SCENE_NAMES",
    "accounting.VOUCHER_STATUS_NAMES", "appointments.APPOINTMENT_STATUS_NAMES", "consents.CORRECTION_STATUS_NAMES",
    "consultations.CONSULTATION_STATUS_NAMES", "cssd.STERILIZATION_STATUS_NAMES",
    "prescriptions.PRESCRIPTION_STATUS_NAMES", "esb.MSG_STATUS", "exams.EXAM_REQUEST_STATUS_NAMES",
    "followups.FOLLOWUP_TASK_STATUS_NAMES", "fund.POOL_STATUS_NAMES", "homevisits.VISIT_ORDER_STATUS_NAMES",
    "materials.PURCHASE_STATUS_NAMES", "materials.CONSUMABLE_STATUS_NAMES", "medication.SHORTAGE_STATUS_NAMES",
    "medwaste.WASTE_STATUS_NAMES", "pathology.SPECIMEN_STATUS", "quality.ADVERSE_EVENT_STATUS_NAMES",
    "referrals.STATUS_LABELS", "surgery.SURGERY_STATUS_NAMES", "tcm.DISPENSE_ORDER_STATUS_NAMES",
    "telemedicine.CONSULT_STATUS_NAMES", "workflows.INSTANCE_STATUS_NAMES",
    "organizations.ORG_LEVEL_NAMES", "accounting.CATEGORY_NAMES", "education.COURSE_CATEGORY_NAMES",
    "tcm_heritage.SIMULATION_CATEGORY_NAMES", "admin_mgmt.DEPT_CATEGORY_NAMES", "admin_mgmt.DOC_TYPE_NAMES",
    "billing.CHARGE_CATEGORY_NAMES", "consents.CORRECTION_TYPE_NAMES",
    "service.MEASUREMENT_SOURCE_NAMES", "service.MEDIA_TYPE_NAMES", "scales.TAG_CATEGORY_NAMES",
    "population.SCREENING_SOURCE_NAMES", "population.GROUP_SCOPE_NAMES", "workbench.TEAM_LEVEL_NAMES",
    "paths.NODE_SERVICE_TYPE_NAMES", "assess.ASSESS_LEVEL_NAMES", "assess.PERIOD_TYPE_NAMES",
    "followup.ENCOUNTER_TYPE_NAMES",
]


def test_文案表名单与参数化一致():
    assert sorted(_label_tables()) == sorted(LABEL_TABLE_NAMES)


@pytest.mark.parametrize("name", LABEL_TABLE_NAMES)
def test_文案表覆盖列注释里的每个状态码(name):
    table, model, column, skipped = _label_tables()[name]
    codes = _column_codes(model, column)
    assert codes, f"{model.__name__}.{column} 的注释里认不出状态码"
    assert set(table) == codes - skipped, (
        f"{name} 与 {model.__name__}.{column} 列注释对不上：缺 {sorted(codes - skipped - set(table))}，"
        f"多 {sorted(set(table) - codes)}——加了新状态要同时补文案，否则页面上又是一个英文码"
    )
    assert all(text and text.strip() for text in table.values()), name


# ================================================================ 端点回归
def test_急救事件带状态文案_随流转而变(client, admin):
    case = client.post("/api/emergency/cases", headers=admin, json={"location": "P272 某路口"})
    assert case.status_code == 201, case.text
    case = case.json()
    assert (case["status"], case["status_name"]) == ("dispatched", "已调度")   # 修前：没有 status_name
    names = []
    for _ in range(3):
        moved = client.post(f"/api/emergency/cases/{case['id']}/advance", headers=admin)
        assert moved.status_code == 200, moved.text
        names.append(moved.json()["status_name"])
    assert names == ["转运中", "已到院", "已收治"]
    judged = client.post(f"/api/emergency/cases/{case['id']}/rescue-outcome", headers=admin,
                         json={"rescue_outcome": "success"})
    assert judged.status_code == 200, judged.text
    assert judged.json()["status_name"] == "已收治"
    row = next(c for c in client.get("/api/emergency/cases", headers=admin).json() if c["id"] == case["id"])
    assert (row["status"], row["status_name"]) == ("admitted", "已收治")


def test_特病申报带状态文案_审核后随之而变(client, admin):
    patient = client.post("/api/patients", headers=admin,
                          json={"name": "P272 特病患者", "id_card": "330192198001014321"}).json()
    applied = client.post("/api/insurance/special-diseases", headers=admin,
                          json={"patient_id": patient["id"], "disease_name": "P272 尿毒症透析"})
    assert applied.status_code == 201, applied.text
    applied = applied.json()
    assert applied["status_name"] == "已申报"   # 修前：页面显示 applied（且是裸插值）
    reviewed = client.post(f"/api/insurance/special-diseases/{applied['id']}/review",
                           params={"approve": "true"}, headers=admin)
    assert reviewed.status_code == 200, reviewed.text
    assert (reviewed.json()["status"], reviewed.json()["status_name"]) == ("approved", "已批准")
    row = next(a for a in client.get("/api/insurance/special-diseases", headers=admin).json()
               if a["id"] == applied["id"])
    assert row["status_name"] == "已批准"


def test_课程_公文_更正申请带类别文案(client, admin):
    """P2-74 ②：没有契约网钉着的三处（课程、公文、更正申请）走一遍——页面原先显示 public_health / minutes / deactivate。"""
    course = client.post("/api/education/courses", headers=admin,
                         json={"title": "P274 公卫课程", "category": "public_health"})
    assert course.status_code == 201, course.text
    assert course.json()["category_name"] == "公共卫生"
    listed = client.get("/api/education/courses", headers=admin).json()
    assert [c["category_name"] for c in listed if c["id"] == course.json()["id"]] == ["公共卫生"]

    doc = client.post("/api/mgmt/docs", headers=admin, json={"title": "P274 纪要", "doc_type": "minutes"})
    assert doc.status_code == 201, doc.text
    assert doc.json()["doc_type_name"] == "会议纪要"
    published = client.post(f"/api/mgmt/docs/{doc.json()['id']}/publish", headers=admin)
    assert published.json()["doc_type_name"] == "会议纪要" and published.json()["status"] == "published"
    docs = client.get("/api/mgmt/docs", headers=admin).json()
    assert [d["doc_type_name"] for d in docs if d["id"] == doc.json()["id"]] == ["会议纪要"]

    patient = client.post("/api/patients", headers=admin, json={"name": "P274 更正患者", "id_card": "330281199001014413"})
    assert patient.status_code in (200, 201), patient.text
    req = client.post("/api/consents/corrections", headers=admin,
                      json={"patient_id": patient.json()["id"], "request_type": "deactivate", "reason": "本人申请注销"})
    assert req.status_code == 201, req.text
    assert req.json()["request_type_name"] == "档案注销"
    pending = client.get("/api/consents/corrections", headers=admin, params={"status": "pending"}).json()
    assert [r["request_type_name"] for r in pending if r["id"] == req.json()["id"]] == ["档案注销"]
