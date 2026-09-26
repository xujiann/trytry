"""全域慢专病 · 智能随访服务端（通用随访能力应用端）+ 智能辅助应用端。

对应招标文件最后两个端：智能随访服务端 #1~#13、智能辅助应用端 #1~#3。

这两个端是**通用能力**而不是慢专病专属：出院随访、术后随访、体检随访对全院
所有科室开放，报告推送也服务于非慢专病业务。所以随访方案 / 问卷 / 呼叫任务
不挂在 `SpdEnrollment` 上——一个刚做完阑尾切除的患者不该为了被随访而先被
"纳管"成慢病患者。它们只认患者与场景。
"""
import zlib
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ... import clock
from ...clock import now_naive
from ...concurrency import insert_if_absent, insert_or_conflict, serialized_on
from ...database import get_db
from ...patchtypes import UNSET
from ...datetypes import OptionalDateStr
from ...deps import (
    get_current_user,
    paginate,
    require_date,
    require_roles,
    resolve_business_date,
    row_dict,
    keyword_like,
)
from ..platform import Admission, Encounter, Organization, Patient, User, unusable_user
from ..models import (
    SpdCallTask,
    SpdFollowupRecord,
    SpdFollowupRule,
    SpdQcSample,
    SpdQuestionnaire,
    SpdReportInstance,
    SpdReportTask,
    SpdReportTemplate,
    SpdRevisit,
    SpdTask,
)
from ..reporting import compose_section, default_period_label
from ..rules import RuleError, as_validated, grade_abnormal
from ..service import (adjust_followup_record, close_followup_record, followup_abnormal, followup_overdue,
                       settle_call_task, spawn_followup_abnormal_task, unknown_code, unknown_ids, unknown_program)
from ...numtypes import INT4_MAX, INT4_MIN
from ...texttypes import NON_BLANK
from ...visibility import assert_org_writable, assert_patient_visible, visible_org_ids

router = APIRouter(
    prefix="/api/spd",
    tags=["全域慢专病·智能随访与辅助"],
    dependencies=[Depends(get_current_user)],
)

FOLLOWUP_ROLES = ("doctor", "public_health", "director", "operator")

#: `spd_followup_records.status` → 中文（§13「状态文案取自后端」，P2-72）：措辞照抄列注释，与随访看板的状态筛选
#: 一致。看板原先只译待随访 / 已完成、其余原样显示，健康日历一个都不译。居民端另有一套说法（「未联系上」），不共用。
FOLLOWUP_STATUS_NAMES = {
    "planned": "待随访", "done": "已完成", "overdue": "已超期", "removed": "已移除", "unreachable": "失访",
}
#: `spd_followup_rules.scene`（记录、问卷的 scene 取同一套码）→ 中文（P2-73）：措辞照抄列注释，与生成随访的场景选项、
#: 居民端「随访类型」一致。随访看板、方案规则、问卷、健康日历、历史随访原先都把场景码原样显示。
FOLLOWUP_SCENE_NAMES = {"inpatient": "出院随访", "outpatient": "门诊随访", "surgery": "术后随访", "checkup": "体检随访"}
#: `spd_followup_records.abnormal_level` → 中文（P2-73）：措辞照抄列注释。看板把 high 原样放进红标签，居民自助作答后
#: 手机上弹的是「系统判定为high异常」，没配处置措施时派出的任务标题是「随访异常处置：high」。
# 就诊类型文案（措辞照抄 Encounter.encounter_type 列注释；随访前置资料「近期就诊」显示它——P2-74）
ENCOUNTER_TYPE_NAMES = {"outpatient": "门诊", "inpatient": "住院"}
ABNORMAL_LEVEL_NAMES = {"none": "无异常", "low": "轻度", "mid": "中度", "high": "重度"}
#: 平台 `admissions.status` → 中文：措辞与平台住院页一致（该页的文案表还在前端，平台出参尚未带文案）。
#: 随访前置资料的住院一栏原先把英文状态码原样显示。
ADMISSION_STATUS_NAMES = {"admitted": "在院", "discharged": "已出院"}


# ============================================================ 响应契约
#
# 模型集中放在所有端点之前（`response_model=` 是装饰器参数，导入时求值）。
# 字段与各 handler 的当前出参**逐字段逐序**对应（治理不得改响应字节，第7条），
# 取值由 tests/test_spd_followup_contract.py 钉住。本模块没有 Money/Float 列：
# 数值只有 Integer 裸 int 与 `round(done/total*100, 1)` 派生的 float
# （completion_rate，0 也是 `0.0`）。`String(10)` 日期列未发生时是空串不是 null
# （executed_at / valid_from / valid_to；last_run_at 同理走 `... else ""`）。


class FollowupRuleOut(BaseModel):
    id: int
    code: str
    name: str
    scene: str
    scene_name: str
    dept: str
    program_code: str
    diagnosis_keywords: list[str]
    surgery_keywords: list[str]
    order_keywords: list[str]
    # 随访时间点（天），[1, 7, 30] 这类原始 JSON 照存照出
    points: list[int]
    questionnaire_code: str
    executor_role: str
    allow_depts: list[str]
    allow_roles: list[str]
    preset: bool
    active: bool


class QuestionnaireOut(BaseModel):
    id: int
    code: str
    name: str
    scene: str
    scene_name: str
    # 题目与异常分级规则是自定义 JSON（{"key","title","type",...} /
    # {"when","level","action"}），照存照出
    items: list[dict[str, Any]]
    abnormal_rules: list[dict[str, Any]]
    track_dept: str
    handle_role: str
    preset: bool
    active: bool


class FollowupRecordOut(BaseModel):
    id: int
    patient_id: int
    # 列表/看板行带患者名；计划回执、历史随访、抽查嵌套里是空串
    patient_name: str
    program_code: str
    rule_id: int | None
    questionnaire_code: str
    scene: str
    scene_name: str
    org_id: int | None
    dept: str
    planned_at: str
    # 未执行是空串不是 null（String(10) 默认 ""）
    executed_at: str
    channel: str
    executor_id: int | None
    answers: dict[str, Any]
    abnormal_level: str
    abnormal_level_name: str
    result: str
    evidence: list[str]
    status: str
    status_name: str
    created_at: str


class FollowupPlanCreatedOut(BaseModel):
    created: int
    items: list[FollowupRecordOut]


class AutoMatchNoRuleOut(BaseModel):
    """自动匹配·无可用方案分支。**键集与扫描分支不同**（没有 scanned、多个 note），
    一个模型排不出两种键集，所以是二选一联合的左支：`extra="forbid"` 让带
    scanned 的扫描分支进不来，反向由扫描分支的必填 scanned 挡住本支——两条分支
    各自按各自的声明序出，字节与治理前一致（契约测试钉住两种键集）。"""

    model_config = ConfigDict(extra="forbid")

    matched: int
    created: int
    note: str


class AutoMatchScanOut(BaseModel):
    """自动匹配·扫描分支：见 `AutoMatchNoRuleOut` 的联合说明。"""

    scanned: int
    matched: int
    created: int


class FollowupExecutedOut(FollowupRecordOut):
    """执行回执：正常分支在**末尾**多一个 `action` 条件键（异常分级的处置措施，
    没配规则时是空串但键在）；失访分支没有它（整个键不出现，不是 null），
    故配 `response_model_exclude_unset=True`。"""

    action: str | None = None


class ContextPatientOut(BaseModel):
    id: int
    name: str
    gender: str
    birth_date: str
    phone: str


class ContextEncounterOut(BaseModel):
    id: int
    encounter_type: str
    encounter_type_name: str
    diagnosis_name: str
    doctor_name: str
    created_at: str


class ContextAdmissionOut(BaseModel):
    id: int
    admitted_at: str
    # 在院是空串不是 null（isoformat() if ... else ""）
    discharged_at: str
    diagnosis_name: str
    doctor_name: str
    status: str
    status_name: str


class FollowupContextOut(BaseModel):
    record: FollowupRecordOut
    # 档案缺失（防御分支）时为 null
    patient: ContextPatientOut | None
    encounters: list[ContextEncounterOut]
    admissions: list[ContextAdmissionOut]
    history: list[FollowupRecordOut]
    # 记录未配问卷或问卷已删时为 null
    questionnaire: QuestionnaireOut | None


class FollowupExecutorStatOut(BaseModel):
    executor_id: int
    executor_name: str
    done: int


class FollowupBoardStatsOut(BaseModel):
    """随访看板统计。与 `spd/workbench` 的 FollowupStatsOut 同名不同形，
    改名避免 OpenAPI 把对方既有 schema 名改写成长限定名。"""

    total: int
    done: int
    completion_rate: float
    overdue: int
    # 判出中度 / 重度异常的随访数（P2-292）：页面「异常随访」卡片原先读它却拿不到，恒显示 0
    abnormal: int
    # 状态/分级/渠道 → 数量：键是状态机取值，随扩充而变，宽键映射
    by_status: dict[str, int]
    by_abnormal: dict[str, int]
    by_channel: dict[str, int]
    by_executor: list[FollowupExecutorStatOut]


class CallDispatchOut(BaseModel):
    accepted: bool
    note: str


class CallTaskCreatedOut(BaseModel):
    id: int
    phone: str
    status: str
    dispatch: CallDispatchOut


class CallResultOut(BaseModel):
    id: int
    status: str
    duration_s: int


class CallTaskRowOut(BaseModel):
    id: int
    patient_id: int
    patient_name: str
    phone: str
    ref_type: str
    ref_id: int | None
    status: str
    duration_s: int
    record_url: str
    result: str
    created_at: str


class QcPlanOut(BaseModel):
    batch: str
    pool: int
    planned: int
    created: int


class QcResultOut(BaseModel):
    id: int
    result: str


class QcSampleRowOut(BaseModel):
    id: int
    record_id: int
    batch: str
    dept: str
    result: str
    method: str
    note: str
    # 被抽的随访记录（患者名空串那种形状）；记录查不到（防御分支）时为 null
    record: FollowupRecordOut | None
    created_at: str


class SpdReportTemplateOut(BaseModel):
    """报告模板。与 `exams` 的 ReportTemplateOut 同名不同形，改名理由同上。"""

    id: int
    code: str
    name: str
    period: str
    scope_level: str
    # [{"key","title","type",...}] 段落配置原始 JSON，照存照出
    sections: list[dict[str, Any]]
    variables: dict[str, Any]
    active: bool


class ReportTaskOut(BaseModel):
    id: int
    template_id: int
    name: str
    frequency: str
    push_time: str
    subscriber_ids: list[int]
    org_ids: list[int]
    valid_from: str
    valid_to: str
    priority: int
    status: str
    # 未跑过是空串不是 null（isoformat() if ... else ""）
    last_run_at: str


class ReportGeneratedOut(BaseModel):
    id: int
    title: str
    period_label: str
    # {"period_label", "sections": [...]}：段落形状由 reporting.py 的注册表
    # 按 key 各自决定（text/table/chart 混形），宽字典照出
    content: dict[str, Any]


class ReportInstanceRowOut(BaseModel):
    id: int
    title: str
    template_code: str
    period_label: str
    scope_level: str
    org_id: int | None
    created_at: str


class ReportInstanceDetailOut(BaseModel):
    """详情：不继承列表行——content/subscriber_ids 插在 org_id 与 created_at
    **之间**，继承追加会把 created_at 排到它们前面去，键序就变了。"""

    id: int
    title: str
    template_code: str
    period_label: str
    scope_level: str
    org_id: int | None
    content: dict[str, Any]
    subscriber_ids: list[int]
    created_at: str


class CalendarRevisitOut(BaseModel):
    id: int
    plan_date: str
    dept: str
    items: str
    status: str


class CalendarTaskOut(BaseModel):
    id: int
    title: str
    task_type: str
    status: str


class HealthCalendarOut(BaseModel):
    day: str
    followups: list[FollowupRecordOut]
    revisits: list[CalendarRevisitOut]
    tasks: list[CalendarTaskOut]


# ============================================================ 随访方案规则


class FollowupRuleIn(BaseModel):
    code: str = Field(min_length=1, max_length=32, pattern=NON_BLANK)
    name: str = Field(min_length=1, max_length=64, pattern=NON_BLANK)
    scene: str = Field(default="inpatient", pattern="^(inpatient|outpatient|surgery|checkup)$")
    dept: str = Field(default="", max_length=64)
    program_code: str = Field(default="", max_length=32)
    diagnosis_keywords: list[str] = Field(default_factory=list)
    surgery_keywords: list[str] = Field(default_factory=list)
    order_keywords: list[str] = Field(default_factory=list)
    points: list[int] = Field(default_factory=list)
    questionnaire_code: str = Field(default="", max_length=32)
    executor_role: str = Field(default="nurse", max_length=32)
    allow_depts: list[str] = Field(default_factory=list)
    allow_roles: list[str] = Field(default_factory=list)


def _check_points(points: list[int]) -> None:
    """建方案与改方案同一句。时间点是按方案生成随访时加在基准日上的天数，
    越界的值让「按方案生成随访」与出院自动匹配在日期运算上 500（P1-94：改方案原先不查）。"""
    if not points:
        raise HTTPException(status_code=422, detail="随访方案至少要有一个随访时间点")
    if any(p < 0 or p > 3650 for p in points):
        raise HTTPException(status_code=422, detail="随访时间点须在 0~3650 天之间")


def _rule_out(r: SpdFollowupRule) -> dict:
    return {
        "id": r.id, "code": r.code, "name": r.name, "scene": r.scene,
        "scene_name": FOLLOWUP_SCENE_NAMES.get(r.scene, r.scene), "dept": r.dept,
        "program_code": r.program_code,
        "diagnosis_keywords": r.diagnosis_keywords or [],
        "surgery_keywords": r.surgery_keywords or [],
        "order_keywords": r.order_keywords or [],
        "points": r.points or [], "questionnaire_code": r.questionnaire_code,
        "executor_role": r.executor_role, "allow_depts": r.allow_depts or [],
        "allow_roles": r.allow_roles or [], "preset": r.preset, "active": r.active,
    }


@router.post("/followup-rules", response_model=FollowupRuleOut, status_code=201,
             dependencies=[Depends(require_roles("director", "doctor"))])
def create_followup_rule(body: FollowupRuleIn, db: Session = Depends(get_db)):
    _check_points(body.points)
    program_problem = unknown_program(db, body.program_code)  # 病种编码先查在不在（P1-120）
    if program_problem:
        raise HTTPException(status_code=404, detail=program_problem)
    # 问卷编码先查在不在（P1-121）：执行随访时按它查问卷，查不到就整段跳过异常分级——高危答案记成「无异常」
    questionnaire_problem = unknown_code(db, SpdQuestionnaire, body.questionnaire_code, "随访问卷")
    if questionnaire_problem:
        raise HTTPException(status_code=404, detail=questionnaire_problem)
    rule = SpdFollowupRule(**body.model_dump())
    db.add(rule)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该随访方案编码已存在") from None
    return _rule_out(rule)


@router.get("/followup-rules", response_model=list[FollowupRuleOut])
def list_followup_rules(
    scene: str | None = None, dept: str | None = None, active: bool | None = None,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """方案清单。带 `allow_depts` / `allow_roles` 的方案只对被授权的科室与角色可见。"""
    query = db.query(SpdFollowupRule)
    if scene:
        query = query.filter(SpdFollowupRule.scene == scene)
    if dept:
        query = query.filter(SpdFollowupRule.dept == dept)
    if active is not None:
        query = query.filter(SpdFollowupRule.active.is_(active))
    rows = query.order_by(SpdFollowupRule.id).limit(300).all()
    if user.role not in ("admin", "director"):
        rows = [
            r for r in rows
            if not (r.allow_roles or []) or user.role in (r.allow_roles or [])
        ]
    return [_rule_out(r) for r in rows]


class FollowupRulePatch(BaseModel):
    """改档与建档同一套约束（P1-94）：原先收裸 dict、照单全收。不传即不改；不可空的列显式传 null 是 422。"""

    name: str = Field(default=UNSET, min_length=1, max_length=64, pattern=NON_BLANK)
    dept: str = Field(default=UNSET, max_length=64)
    program_code: str = Field(default=UNSET, max_length=32)
    diagnosis_keywords: list[str] = Field(default=UNSET)
    surgery_keywords: list[str] = Field(default=UNSET)
    order_keywords: list[str] = Field(default=UNSET)
    points: list[int] = Field(default=UNSET)
    questionnaire_code: str = Field(default=UNSET, max_length=32)
    executor_role: str = Field(default=UNSET, max_length=32)
    allow_depts: list[str] = Field(default=UNSET)
    allow_roles: list[str] = Field(default=UNSET)
    active: bool = Field(default=UNSET)


@router.patch("/followup-rules/{rule_id}", response_model=FollowupRuleOut,
              dependencies=[Depends(require_roles("director", "doctor"))])
def update_followup_rule(rule_id: int, body: FollowupRulePatch, db: Session = Depends(get_db)):
    rule = db.get(SpdFollowupRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="随访方案不存在")
    changes = body.model_dump(exclude_unset=True)
    if "points" in changes:
        _check_points(changes["points"])
    # 病种与问卷编码先查在不在（P1-120 / P1-121）；与现值相同的不再查——编辑页每次都带上原问卷编码，
    # 存量里悬空的编码不该挡住改名、停用
    program_problem = unknown_program(db, changes.get("program_code") or "", already=rule.program_code)
    if program_problem:
        raise HTTPException(status_code=404, detail=program_problem)
    questionnaire_problem = unknown_code(db, SpdQuestionnaire, changes.get("questionnaire_code") or "", "随访问卷",
                                         already=rule.questionnaire_code)
    if questionnaire_problem:
        raise HTTPException(status_code=404, detail=questionnaire_problem)
    for key, value in changes.items():
        setattr(rule, key, value)
    db.commit()
    return _rule_out(rule)


# ============================================================ 随访问卷


class QuestionnaireIn(BaseModel):
    code: str = Field(min_length=1, max_length=32, pattern=NON_BLANK)
    name: str = Field(min_length=1, max_length=64, pattern=NON_BLANK)
    scene: str = Field(default="inpatient", max_length=16)
    items: list[dict] = Field(default_factory=list)
    abnormal_rules: list[dict] = Field(default_factory=list)
    track_dept: str = Field(default="", max_length=64)
    handle_role: str = Field(default="doctor", max_length=32)


#: 异常分级规则的级别（`grade_abnormal` 的级别序 none < low < mid < high；none 是「没命中」，不是规则的级别）
ABNORMAL_RULE_LEVELS = ("low", "mid", "high")


def _check_abnormal_rules(rules: list[dict], items: list[dict]) -> list[dict]:
    """建问卷与改问卷同一句：异常分级规则在随访结案时逐条求值，写坏的规则让结案 500（P1-94：改问卷原先不查）。

    P1-122 起连同题目一起查：执行随访按题目 key 把作答交给规则求值，规则引用问卷里没有的题目（`pain` 写成
    `pian`）、级别写成表外的值，都照收却永远判不出异常——疼痛 9 分记成「无异常」、不派处置任务，没有任何报错。
    所以题目必须有 key 且不重复，规则的字段必须是本问卷的题目，级别只能是轻 / 中 / 重三档。

    返回**要存的规则**（P2-290）：字段 / 比较符按去掉两端空格后的值查、原先却存原样，「pain 」过了校验、结案时
    按原样比，永远不命中——查的是哪个就存哪个。
    """
    keys = [item.get("key") if isinstance(item, dict) else None for item in items]
    if any(not isinstance(key, str) or not key.strip() for key in keys):
        raise HTTPException(status_code=422, detail="问卷的每道题都要有 key")
    if len(keys) != len(set(keys)):
        raise HTTPException(status_code=422, detail="问卷题目 key 不得重复")
    checked = []
    for rule in rules:
        try:
            (cond,) = as_validated([rule.get("when", {})])
        except RuleError as exc:
            raise HTTPException(status_code=422, detail=f"异常分级规则非法：{exc}") from None
        if cond["field"] not in keys:
            raise HTTPException(status_code=422, detail=f"异常分级规则引用了问卷里没有的题目：{cond['field']}")
        if rule.get("level", "low") not in ABNORMAL_RULE_LEVELS:
            raise HTTPException(status_code=422,
                                detail="异常分级规则的级别只能是 low（轻度）/ mid（中度）/ high（重度）")
        checked.append({**rule, "when": cond})
    return checked


def _q_out(q: SpdQuestionnaire) -> dict:
    return {
        "id": q.id, "code": q.code, "name": q.name, "scene": q.scene,
        "scene_name": FOLLOWUP_SCENE_NAMES.get(q.scene, q.scene),
        "items": q.items or [], "abnormal_rules": q.abnormal_rules or [],
        "track_dept": q.track_dept, "handle_role": q.handle_role,
        "preset": q.preset, "active": q.active,
    }


@router.post("/questionnaires", response_model=QuestionnaireOut, status_code=201,
             dependencies=[Depends(require_roles("director", "doctor"))])
def create_questionnaire(body: QuestionnaireIn, db: Session = Depends(get_db)):
    questionnaire = SpdQuestionnaire(**{**body.model_dump(),
                                        "abnormal_rules": _check_abnormal_rules(body.abnormal_rules, body.items)})
    db.add(questionnaire)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该问卷编码已存在") from None
    return _q_out(questionnaire)


@router.get("/questionnaires", response_model=list[QuestionnaireOut])
def list_questionnaires(
    response: Response,
    scene: str | None = None,
    include_inactive: bool = False,
    offset: int = 0,
    limit: int = 200,
    db: Session = Depends(get_db),
):
    """问卷目录。缺省只列启用的（选问卷的下拉用）；`include_inactive` 连停用的一起列（P2-294）——随访页的问卷管理表
    原先也只拿得到启用的，停用的从表里消失，表上的「停用」标签与编辑里的「启用」永远用不上，停了就启不回来。"""
    query = db.query(SpdQuestionnaire)
    if not include_inactive:
        query = query.filter(SpdQuestionnaire.active.is_(True))
    if scene:
        query = query.filter(SpdQuestionnaire.scene == scene)
    return [
        _q_out(q)
        for q in paginate(query.order_by(SpdQuestionnaire.id), response, offset, limit)
    ]


class QuestionnairePatch(BaseModel):
    """改档与建档同一套约束（P1-94）：原先收裸 dict、照单全收。不传即不改；不可空的列显式传 null 是 422。"""

    name: str = Field(default=UNSET, min_length=1, max_length=64, pattern=NON_BLANK)
    items: list[dict] = Field(default=UNSET)
    abnormal_rules: list[dict] = Field(default=UNSET)
    track_dept: str = Field(default=UNSET, max_length=64)
    handle_role: str = Field(default=UNSET, max_length=32)
    active: bool = Field(default=UNSET)


@router.patch("/questionnaires/{q_id}", response_model=QuestionnaireOut,
              dependencies=[Depends(require_roles("director", "doctor"))])
def update_questionnaire(q_id: int, body: QuestionnairePatch, db: Session = Depends(get_db)):
    questionnaire = db.get(SpdQuestionnaire, q_id)
    if questionnaire is None:
        raise HTTPException(status_code=404, detail="问卷不存在")
    changes = body.model_dump(exclude_unset=True)
    # 题目与规则合起来查（P1-122）：只改题目也可能让原有规则引用的题目不复存在；两样都没变不查——
    # 存量里已经写坏的问卷，改名、停用不该被它挡住
    items = changes.get("items", questionnaire.items or [])
    rules = changes.get("abnormal_rules", questionnaire.abnormal_rules or [])
    if (items, rules) != (questionnaire.items or [], questionnaire.abnormal_rules or []):
        changes["abnormal_rules"] = _check_abnormal_rules(rules, items)   # 查的是哪个就存哪个（P2-290）
    for key, value in changes.items():
        setattr(questionnaire, key, value)
    db.commit()
    return _q_out(questionnaire)


# ============================================================ 随访任务生成与执行


class GeneratePlanIn(BaseModel):
    patient_id: int
    rule_id: int
    base_date: OptionalDateStr = ""
    org_id: int | None = None
    dept: str = Field(default="", max_length=64)
    executor_id: int | None = None
    channel: str = Field(default="phone", pattern="^(phone|wechat|sms|self|visit)$")


def _record_out(r: SpdFollowupRecord, patient_name: str = "") -> dict:
    return {
        "id": r.id, "patient_id": r.patient_id, "patient_name": patient_name,
        "program_code": r.program_code, "rule_id": r.rule_id,
        "questionnaire_code": r.questionnaire_code, "scene": r.scene,
        "scene_name": FOLLOWUP_SCENE_NAMES.get(r.scene, r.scene), "org_id": r.org_id, "dept": r.dept, "planned_at": r.planned_at,
        "executed_at": r.executed_at, "channel": r.channel,
        "executor_id": r.executor_id, "answers": r.answers or {},
        "abnormal_level": r.abnormal_level,
        "abnormal_level_name": ABNORMAL_LEVEL_NAMES.get(r.abnormal_level, r.abnormal_level), "result": r.result,
        "evidence": r.evidence or [], "status": r.status,
        "status_name": FOLLOWUP_STATUS_NAMES.get(r.status, r.status),
        "created_at": r.created_at.isoformat(),
    }


@router.post("/followup-plans", response_model=FollowupPlanCreatedOut, status_code=201,
             dependencies=[Depends(require_roles(*FOLLOWUP_ROLES))])
def generate_followup_plan(
    body: GeneratePlanIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """按方案的多个时间点一次性生成随访任务（智能随访端 #4）。

    基准日缺省取今天；出院随访应当传出院日，术后随访传手术日——
    接口不去猜是哪一天，因为猜错的后果是整条随访计划全部错位。
    """
    assert_patient_visible(db, user, body.patient_id, resource="spd_followup")
    # P0-43（P0-35 判据第二层）：随访任务归哪家机构由请求声明，原先守着它的只有上面这句「患者看不看得见」——
    # 乙院医生接诊过的患者，就能把随访派进甲院的随访队列（实测 201）。与下面的自动匹配同一口径。
    org_id = body.org_id if body.org_id is not None else user.org_id
    assert_org_writable(db, user, org_id)
    rule = db.get(SpdFollowupRule, body.rule_id)
    if rule is None or not rule.active:
        raise HTTPException(status_code=404, detail="随访方案不存在或已停用")
    # 执行人先查存在（P1-90）：不查的话开发库存成悬空 id，生产库撞外键直接 500；停用的账号也不收——
    # 一整条随访计划派给登录不了的人，到点没人做（P1-106）
    if body.executor_id is not None:
        state = unusable_user(db, body.executor_id)
        if state:
            raise HTTPException(status_code=404, detail=f"随访执行人{state}（executor_id={body.executor_id}）")
    base = date.fromisoformat(body.base_date) if body.base_date else clock.today()
    created = []
    for offset in rule.points or []:
        record = SpdFollowupRecord(
            patient_id=body.patient_id, program_code=rule.program_code, rule_id=rule.id,
            questionnaire_code=rule.questionnaire_code, scene=rule.scene,
            org_id=org_id,
            dept=body.dept or rule.dept,
            planned_at=(base + timedelta(days=int(offset))).isoformat(),
            channel=body.channel, executor_id=body.executor_id, status="planned",
        )
        db.add(record)
        created.append(record)
    db.commit()
    return {"created": len(created), "items": [_record_out(r) for r in created]}


class AutoMatchIn(BaseModel):
    scene: str = Field(default="inpatient", pattern="^(inpatient|outpatient|surgery|checkup)$")
    org_id: int | None = None
    days: int = Field(default=7, ge=1, le=90)
    limit: int = Field(default=200, ge=1, le=1000)


@router.post("/followup-plans/auto-match",
             response_model=AutoMatchNoRuleOut | AutoMatchScanOut,
             dependencies=[Depends(require_roles(*FOLLOWUP_ROLES))])
def auto_match_plans(
    body: AutoMatchIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """依据患者特征自动匹配方案并生成随访任务（智能随访端 #5）。

    匹配靠诊断关键词命中：方案没配任何关键词就是**不匹配任何人**（与纳入规则
    同一口径），否则一个空方案会给全院每个出院患者都排上随访。

    回溯窗口 `days` 四个场景同一口径：出院场景按出院时间、其余按就诊时间取近 N 天。出院场景原先不看它，
    取的是最近建档的已出院记录（两年前出院的也在内），随访日期加在出院日上，排出来就是一批早已超期的随访（P1-134）。
    """
    org_id = body.org_id if body.org_id is not None else user.org_id
    if org_id is None:
        # 住院与就诊记录都必挂机构：全域账号（没有本机构）不指定机构，原先按「机构为空」去扫，恒 0 条、不报任何原因（P2-92）
        raise HTTPException(status_code=422, detail="请指定按哪家机构的出院 / 就诊记录匹配（本账号没有所属机构）")
    # P0-35：给了 org_id 就照单全收——乙院能以甲院名义按甲院的出院 / 门诊患者批量生成随访。
    assert_org_writable(db, user, org_id)
    rules = (
        db.query(SpdFollowupRule)
        .filter(SpdFollowupRule.scene == body.scene, SpdFollowupRule.active.is_(True))
        .all()
    )
    rules = [r for r in rules if (r.diagnosis_keywords or [])]
    if not rules:
        return {"matched": 0, "created": 0, "note": "没有配置了诊断关键词的可用方案"}

    since = now_naive() - timedelta(days=body.days)
    if body.scene == "inpatient":
        rows = (
            db.query(Admission)
            .filter(Admission.org_id == org_id, Admission.status == "discharged",
                    func.coalesce(Admission.discharged_at, Admission.admitted_at) >= since)
            .order_by(Admission.id.desc())
            .limit(body.limit)
            .all()
        )
        candidates = [
            (
                a.patient_id,
                (a.discharged_at or a.admitted_at).date().isoformat(),
                a.diagnosis_name or "",
            )
            for a in rows
        ]
    else:
        encounters = (
            db.query(Encounter)
            .filter(Encounter.org_id == org_id, Encounter.created_at >= since)
            .order_by(Encounter.id.desc())
            .limit(body.limit)
            .all()
        )
        candidates = [
            (e.patient_id, e.created_at.date().isoformat(),
             f"{e.diagnosis_name or ''}{e.diagnosis_code or ''}")
            for e in encounters
        ]

    matched, created = 0, 0
    # 本次扫描已经看过的（患者, 方案）：会话不自动 flush，同一位患者第二次命中时，下面的查库看不见刚 add 的
    # 那份计划，会再排一整份（近几天两次就诊、一周内两次出院，P1-134）。候选按新到旧排，留下的是最近那一次。
    seen: set[tuple[int, int]] = set()
    for patient_id, base_date, text in candidates:
        rule = next(
            (r for r in rules if any(k and k in text for k in r.diagnosis_keywords or [])), None
        )
        if rule is None:
            continue
        matched += 1
        if (patient_id, rule.id) in seen:
            continue
        seen.add((patient_id, rule.id))
        exists = (
            db.query(SpdFollowupRecord.id)
            .filter(
                SpdFollowupRecord.patient_id == patient_id,
                SpdFollowupRecord.rule_id == rule.id,
            )
            .first()
        )
        if exists is not None:
            continue
        try:
            base = date.fromisoformat(base_date)
        except (ValueError, TypeError):
            base = clock.today()
        for offset in rule.points or []:
            db.add(
                SpdFollowupRecord(
                    patient_id=patient_id, program_code=rule.program_code, rule_id=rule.id,
                    questionnaire_code=rule.questionnaire_code, scene=rule.scene,
                    org_id=org_id, dept=rule.dept,
                    planned_at=(base + timedelta(days=int(offset))).isoformat(),
                    channel="phone", status="planned",
                )
            )
            created += 1
    db.commit()
    return {"scanned": len(candidates), "matched": matched, "created": created}


@router.get("/followup-records", response_model=list[FollowupRecordOut])
def list_followup_records(
    response: Response,
    patient_id: int | None = None,
    scene: str | None = None,
    status: str | None = None,
    dept: str | None = None,
    executor_id: int | None = None,
    abnormal_level: str | None = None,
    mine: bool = False,
    date_from: str = "",
    date_to: str = "",
    overdue: bool = False,
    today: str | None = None,
    offset: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """随访看板：全院 / 科室 / 个人三个口径由参数组合而成，不做三个接口。

    `overdue=true` 按 status 过滤（进接口先跑一次超期扫描）——与督办、考核同一口径。
    """
    business_day = resolve_business_date(today)
    if overdue:
        from ..service import sweep_overdue_on_read

        sweep_overdue_on_read(db, business_day)
        db.commit()
    query = db.query(SpdFollowupRecord)
    if patient_id is not None:
        assert_patient_visible(db, user, patient_id, resource="spd_followup")
        query = query.filter(SpdFollowupRecord.patient_id == patient_id)
    else:
        orgs = visible_org_ids(db, user)
        if orgs is not None:
            query = query.filter(SpdFollowupRecord.org_id.in_(orgs))
    if mine:
        query = query.filter(SpdFollowupRecord.executor_id == user.id)
    elif executor_id is not None:
        query = query.filter(SpdFollowupRecord.executor_id == executor_id)
    for column, value in (
        (SpdFollowupRecord.scene, scene), (SpdFollowupRecord.status, status),
        (SpdFollowupRecord.dept, dept),
        (SpdFollowupRecord.abnormal_level, abnormal_level),
    ):
        if value is not None and value != "":
            query = query.filter(column == value)
    if date_from:
        date_from = require_date(date_from, field="date_from")
        query = query.filter(SpdFollowupRecord.planned_at >= date_from)
    if date_to:
        date_to = require_date(date_to, field="date_to")
        query = query.filter(SpdFollowupRecord.planned_at <= date_to)
    if overdue:
        query = query.filter(SpdFollowupRecord.status == "overdue")
    rows = paginate(
        query.order_by(SpdFollowupRecord.planned_at, SpdFollowupRecord.id),
        response, offset, limit,
    )
    names = {
        p.id: p.name
        for p in db.query(Patient).filter(Patient.id.in_([r.patient_id for r in rows] or [0]))
    }
    return [_record_out(r, names.get(r.patient_id, "")) for r in rows]


@router.get("/followup-records/{record_id}/context", response_model=FollowupContextOut)
def followup_context(
    record_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """随访前置资料（智能随访端 #1）：基本资料 + 就诊 + 住院 + 历史随访一屏聚合。"""
    record = db.get(SpdFollowupRecord, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="随访记录不存在")
    assert_patient_visible(db, user, record.patient_id, resource="spd_followup")
    patient = db.get(Patient, record.patient_id)
    encounters = (
        db.query(Encounter)
        .filter(Encounter.patient_id == record.patient_id)
        .order_by(Encounter.id.desc())
        .limit(10)
        .all()
    )
    admissions = (
        db.query(Admission)
        .filter(Admission.patient_id == record.patient_id)
        .order_by(Admission.id.desc())
        .limit(5)
        .all()
    )
    history = (
        db.query(SpdFollowupRecord)
        .filter(
            SpdFollowupRecord.patient_id == record.patient_id,
            SpdFollowupRecord.status == "done",
        )
        .order_by(SpdFollowupRecord.id.desc())
        .limit(10)
        .all()
    )
    questionnaire = (
        db.query(SpdQuestionnaire)
        .filter(SpdQuestionnaire.code == record.questionnaire_code)
        .first()
    )
    return {
        "record": _record_out(record, patient.name if patient else ""),
        "patient": {
            "id": patient.id, "name": patient.name, "gender": patient.gender,
            "birth_date": patient.birth_date, "phone": patient.phone,
        } if patient else None,
        "encounters": [
            {"id": e.id, "encounter_type": e.encounter_type,
             "encounter_type_name": ENCOUNTER_TYPE_NAMES.get(e.encounter_type, e.encounter_type),
             "diagnosis_name": e.diagnosis_name, "doctor_name": e.doctor_name,
             "created_at": e.created_at.isoformat()}
            for e in encounters
        ],
        "admissions": [
            {"id": a.id, "admitted_at": a.admitted_at.isoformat(),
             "discharged_at": a.discharged_at.isoformat() if a.discharged_at else "",
             "diagnosis_name": a.diagnosis_name, "doctor_name": a.doctor_name,
             "status": a.status, "status_name": ADMISSION_STATUS_NAMES.get(a.status, a.status)}
            for a in admissions
        ],
        "history": [_record_out(h) for h in history],
        "questionnaire": _q_out(questionnaire) if questionnaire else None,
    }


class ExecuteIn(BaseModel):
    answers: dict = Field(default_factory=dict)
    channel: str = Field(default="phone", pattern="^(phone|wechat|sms|self|visit)$")
    result: str = Field(default="", max_length=512)
    evidence: list[str] = Field(default_factory=list)
    unreachable: bool = False


@router.post("/followup-records/{record_id}/execute",
             response_model=FollowupExecutedOut, response_model_exclude_unset=True,
             dependencies=[Depends(require_roles(*FOLLOWUP_ROLES))])
def execute_followup(
    record_id: int, body: ExecuteIn, db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """执行随访：填问卷 → 自动异常分级 → 中度 / 重度异常自动派处置任务（与居民自助作答同一个派单帮手）。

    失访（`unreachable`）单独一个状态而不是"完成但没答案"：随访完成率的分母
    应该含失访、分子不含，两者混在一起会把完成率算高。
    """
    record = db.get(SpdFollowupRecord, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="随访记录不存在")
    assert_org_writable(db, user, record.org_id)
    if record.status in ("done", "removed"):
        raise HTTPException(status_code=409, detail="该随访已结束")
    # 结果与证据是追加、不是整段覆盖（P2-291）：接通的呼叫回写早把沟通结果与录音地址追加在这条记录上（「回写通话
    # 结果」写明「接通结果会同步写回随访记录」），执行时整段覆盖——界面上随访结果留空也照样覆盖——就把它们抹掉了。
    # 与呼叫回写同一个临界区（锁这一行、重读、再追加）：先到的回写这里重读得到，后到的回写锁到手时看到已办结、不再追加
    with serialized_on(db, SpdFollowupRecord, record.id):
        db.refresh(record)
        record.channel = body.channel
        record.executor_id = user.id
        record.executed_at = clock.today().isoformat()
        record.result = (record.result + " " + body.result).strip()[:512]
        if body.evidence:
            record.evidence = (record.evidence or []) + body.evidence
        # 上面那道预检是 check-then-act：两名随访人员（或医护与居民自助）同时执行同一条
        # 随访，都读到 planned 就都办结、都派一条处置任务。终态跃迁改成条件 UPDATE——
        # 判定与写在同一条 SQL 里，抢输的一路 rowcount 为 0，拿到与预检完全一致的 409；
        # 处置任务只在跃迁命中之后才派（`spd_tasks` 上没有指向随访记录的列，
        # "一次执行只派一条任务"只能守在父行的状态上，见 service.close_followup_record）。
        if not close_followup_record(
            db, record.id, "unreachable" if body.unreachable else "done",
            allowed_from=("planned", "overdue", "unreachable"),
        ):
            db.rollback()  # 先退掉本请求的写事务，再抛——否则后续审计落库会撞写锁
            raise HTTPException(status_code=409, detail="该随访已结束")
        if body.unreachable:
            db.commit()
            return _record_out(record)

        record.answers = body.answers
        questionnaire = (
            db.query(SpdQuestionnaire)
            .filter(SpdQuestionnaire.code == record.questionnaire_code)
            .first()
        )
        action = ""
        if questionnaire is not None:
            level, action = grade_abnormal(questionnaire.abnormal_rules or [], body.answers)
            record.abnormal_level = level
            spawn_followup_abnormal_task(
                db, record, level, f"随访异常处置：{action or ABNORMAL_LEVEL_NAMES.get(level, level) + '异常'}")
        db.commit()
    out = _record_out(record)
    out["action"] = action
    return out


class RecordPatchIn(BaseModel):
    # 不可空的列可以不传、不能传 null（P1-95，写法见 app/patchtypes.py）：原先显式 null 照写进 NOT NULL 列，500
    status: str = Field(default=UNSET, pattern="^(planned|removed)$")
    planned_at: OptionalDateStr = Field(default=UNSET)
    executor_id: int | None = None
    # 与执行随访、生成计划同一条枚举（P1-98）：原先改档可把渠道改成枚举外的任意串，按渠道统计对不上
    channel: str = Field(default=UNSET, pattern="^(phone|wechat|sms|self|visit)$")


@router.patch("/followup-records/{record_id}", response_model=FollowupRecordOut,
              dependencies=[Depends(require_roles(*FOLLOWUP_ROLES))])
def update_followup_record(
    record_id: int,
    body: RecordPatchIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """移除 / 恢复随访任务、改期、改执行人（智能随访端 #4"任务移除恢复"）。"""
    record = db.get(SpdFollowupRecord, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="随访记录不存在")
    assert_org_writable(db, user, record.org_id)
    if record.status == "done":
        raise HTTPException(status_code=409, detail="已完成的随访不可修改")
    changes = body.model_dump(exclude_unset=True)
    if changes.get("executor_id") is not None and changes["executor_id"] != record.executor_id:  # 同上，与现值相同的不再查
        state = unusable_user(db, changes["executor_id"])
        if state:
            raise HTTPException(status_code=404,
                                detail=f"随访执行人{state}（executor_id={changes['executor_id']}）")
    # 改的列与「还没完成」同一条条件 UPDATE（P2-287）：上面那道预检是锁外读的，这期间别人刚执行完的随访不能被改回去
    if changes and not adjust_followup_record(db, record_id, **changes):
        db.rollback()
        raise HTTPException(status_code=409, detail="已完成的随访不可修改")
    db.commit()
    db.refresh(record)
    return _record_out(record)


@router.get("/followup-stats", response_model=FollowupBoardStatsOut)
def followup_stats(
    dept: str | None = None,
    scene: str | None = None,
    executor_id: int | None = None,
    date_from: str = "",
    date_to: str = "",
    today: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """随访质量与工作量统计（智能随访端 #13）：完成率、超期、异常分布、人员工作量。"""
    business_day = resolve_business_date(today)
    query = db.query(SpdFollowupRecord)
    orgs = visible_org_ids(db, user)
    if orgs is not None:
        query = query.filter(SpdFollowupRecord.org_id.in_(orgs))
    if dept:
        query = query.filter(SpdFollowupRecord.dept == dept)
    if scene:
        query = query.filter(SpdFollowupRecord.scene == scene)
    if executor_id is not None:
        query = query.filter(SpdFollowupRecord.executor_id == executor_id)
    if date_from:
        date_from = require_date(date_from, field="date_from")
        query = query.filter(SpdFollowupRecord.planned_at >= date_from)
    if date_to:
        date_to = require_date(date_to, field="date_to")
        query = query.filter(SpdFollowupRecord.planned_at <= date_to)

    by_status = row_dict(
        query.with_entities(SpdFollowupRecord.status, func.count(SpdFollowupRecord.id))
        .group_by(SpdFollowupRecord.status)
        .order_by(SpdFollowupRecord.status).all()
    )
    total = sum(by_status.values())
    done = by_status.get("done", 0)
    # planned 且已过期的算上（扫描间隙里的）+ 已标 overdue 的，两者都是超期（判定与工作台共用一处，P1-128）
    overdue = query.filter(followup_overdue(business_day.isoformat())).count()
    by_abnormal = row_dict(
        query.filter(SpdFollowupRecord.status == "done")
        .with_entities(SpdFollowupRecord.abnormal_level, func.count(SpdFollowupRecord.id))
        .group_by(SpdFollowupRecord.abnormal_level)
        .order_by(SpdFollowupRecord.abnormal_level).all()
    )
    by_executor = (
        query.filter(SpdFollowupRecord.status == "done")
        .with_entities(SpdFollowupRecord.executor_id, func.count(SpdFollowupRecord.id))
        .group_by(SpdFollowupRecord.executor_id)
        .order_by(SpdFollowupRecord.executor_id).all()
    )
    names = {
        u.id: u.full_name or u.username
        for u in db.query(User).filter(User.id.in_([e for e, _ in by_executor if e] or [0]))
    }
    return {
        "total": total,
        "done": done,
        "completion_rate": round(done / total * 100, 1) if total else 0.0,
        "overdue": overdue,
        "abnormal": query.filter(followup_abnormal()).count(),   # 与工作台同一个判定（P2-292）
        "by_status": by_status,
        "by_abnormal": by_abnormal,
        "by_channel": row_dict(
            query.filter(SpdFollowupRecord.status == "done")
            .with_entities(SpdFollowupRecord.channel, func.count(SpdFollowupRecord.id))
            .group_by(SpdFollowupRecord.channel)
            .order_by(SpdFollowupRecord.channel).all()
        ),
        "by_executor": [
            {"executor_id": eid, "executor_name": names.get(eid, ""), "done": count}
            for eid, count in by_executor if eid
        ],
    }


# ============================================================ 呼叫任务与录音


class CallTaskIn(BaseModel):
    patient_id: int
    phone: str = Field(default="", max_length=20)
    ref_type: str = Field(default="followup", max_length=24)
    ref_id: int | None = Field(default=None, ge=INT4_MIN, le=INT4_MAX)


@router.post("/call-tasks", response_model=CallTaskCreatedOut, status_code=201,
             dependencies=[Depends(require_roles(*FOLLOWUP_ROLES))])
def create_call_task(
    body: CallTaskIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """把随访 / 复诊 / 宣教 / 异常处置转为呼叫任务（智能随访端 #10）。"""
    assert_patient_visible(db, user, body.patient_id, resource="spd_call")
    phone = body.phone
    if not phone:
        patient = db.get(Patient, body.patient_id)
        phone = patient.phone if patient else ""
    task = SpdCallTask(
        patient_id=body.patient_id, phone=phone, ref_type=body.ref_type,
        ref_id=body.ref_id, operator_id=user.id, status="pending",
    )
    # 先落库再派发。同一患者、同一被引用对象（某条随访/复诊）上只许有一条**待呼叫**
    # 任务：双击「转呼叫」或两名坐席同时发起，旧写法会静默建出两条 pending——网关被
    # 推两次（患者被拨两遍）、人工队列同一条随访出现两行，先接通后另一条永远挂着等
    # 人手工取消。部分唯一索引 uq_spd_call_task_pending_ref 兜底，抢输的一路在此 409。
    # 先提交也让网关拿到 task_id 立刻回调 result 时查得到这一行（旧写法 flush 未提交）。
    insert_or_conflict(
        db, task, "该患者对同一对象已有待呼叫任务，请先回写其结果（未接通/取消）后再发起"
    )
    # 经呼叫通道派发（manual=等人工外呼，http=推给呼叫中心）。
    # 派发失败不报错：任务留在 pending、结果里记原因——通道抖一下
    # 不该让"发起随访"这个动作失败。
    from ..callcenter import get_call_provider

    accepted, note = get_call_provider().dispatch(task.id, phone, body.ref_type)
    if not accepted:
        task.result = note
        db.commit()
    return {"id": task.id, "phone": task.phone, "status": task.status,
            "dispatch": {"accepted": accepted, "note": note}}


class CallResultIn(BaseModel):
    status: str = Field(pattern="^(connected|failed|cancelled)$")
    duration_s: int = Field(default=0, ge=0, le=36000)
    record_url: str = Field(default="", max_length=256)
    result: str = Field(default="", max_length=512)


@router.post("/call-tasks/{task_id}/result", response_model=CallResultOut,
             dependencies=[Depends(require_roles(*FOLLOWUP_ROLES))])
def record_call_result(
    task_id: int, body: CallResultIn, db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """回写通话结果与录音地址；接通结果同步写回关联的随访记录。"""
    task = db.get(SpdCallTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="呼叫任务不存在")
    # 结果只回写一次（P2-87）：页面只对待呼叫的任务给「回写结果」，网关每条任务回调一次（呼叫失败后重派是新的一行）。
    # 已有结果的再写一次，只会把接通的通话、要回听的录音地址与沟通结果事后改掉
    if task.status != "pending":
        raise HTTPException(status_code=409, detail="该呼叫任务已回写过结果")
    record = None
    if body.status == "connected" and task.ref_type == "followup" and task.ref_id:
        record = db.get(SpdFollowupRecord, task.ref_id)
        if record is not None:
            assert_org_writable(db, user, record.org_id)
    def settle() -> None:
        # 上面那道预检是锁外读的（P2-289）：重发的回调、回调与坐席手工回写同时到，两路都读到待呼叫——翻转压进一条
        # `WHERE status = 'pending'` 的 UPDATE，后到的一路 409、不再往随访记录上追加
        if not settle_call_task(
            db, task.id, status=body.status, duration_s=body.duration_s, record_url=body.record_url,
            result=body.result, started_at=func.coalesce(SpdCallTask.started_at, now_naive()),
            operator_id=func.coalesce(SpdCallTask.operator_id, user.id),
        ):
            db.rollback()
            raise HTTPException(status_code=409, detail="该呼叫任务已回写过结果")

    if record is not None and record.status in ("planned", "overdue"):
        # 结果串与证据列表都是"读旧值 + 本次 → 整体写回"：同一条随访记录挂着的两个
        # 呼叫任务同时回写，后写的把先写的结果与录音地址盖掉。锁住随访记录这一行、
        # 重读、再追加（concurrency.serialized_on）；锁到手后若已被别人办结就不再往上写。
        # 先进临界区、再翻呼叫任务（P2-291）：执行随访也在这一行的临界区里写，两处都先锁随访记录、后写库，
        # 取锁顺序一致（SQLite 上反过来是库写锁与进程内锁交叉等待，等满超时一路 500）
        with serialized_on(db, SpdFollowupRecord, record.id):
            settle()
            db.refresh(record)
            if record.status in ("planned", "overdue"):
                record.result = (record.result + " " + body.result).strip()[:500]
                record.evidence = (record.evidence or []) + (
                    [body.record_url] if body.record_url else []
                )
            db.commit()
    else:
        settle()
        db.commit()
    db.refresh(task)
    return {"id": task.id, "status": task.status, "duration_s": task.duration_s}


@router.get("/call-tasks", response_model=list[CallTaskRowOut])
def list_call_tasks(
    response: Response,
    status: str | None = None,
    phone: str = "",
    patient_name: str = "",
    date_from: str = "",
    date_to: str = "",
    offset: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    query = db.query(SpdCallTask)
    if status:
        query = query.filter(SpdCallTask.status == status)
    if phone:
        query = query.filter(SpdCallTask.phone.contains(phone))
    if patient_name:
        # 子查询而不是先取患者号：原先 `.limit(200)` 取任意 200 个同名患者再筛，常见姓氏一搜名单少一截（P1-83）
        query = query.filter(SpdCallTask.patient_id.in_(select(Patient.id).where(keyword_like(Patient.name, patient_name))))
    # 先校验再拼串：非法值拼成的时间戳在真 PG 上转换失败是 500（P1-58）
    if date_from:
        date_from = require_date(date_from, field="date_from")
        query = query.filter(SpdCallTask.created_at >= f"{date_from} 00:00:00")
    if date_to:
        date_to = require_date(date_to, field="date_to")
        query = query.filter(SpdCallTask.created_at <= f"{date_to} 23:59:59")
    rows = paginate(query.order_by(SpdCallTask.id.desc()), response, offset, limit)
    names = {
        p.id: p.name
        for p in db.query(Patient).filter(Patient.id.in_([r.patient_id for r in rows] or [0]))
    }
    return [
        {"id": r.id, "patient_id": r.patient_id, "patient_name": names.get(r.patient_id, ""),
         "phone": r.phone, "ref_type": r.ref_type, "ref_id": r.ref_id,
         "status": r.status, "duration_s": r.duration_s, "record_url": r.record_url,
         "result": r.result, "created_at": r.created_at.isoformat()}
        for r in rows
    ]


# ============================================================ 随访抽查质控


class QcPlanIn(BaseModel):
    dept: str = Field(default="", max_length=64)
    ratio: float = Field(default=0.1, gt=0, le=1)
    count: int = Field(default=0, ge=0, le=500)
    batch: str = Field(default="", max_length=32)


#: 抽样散列的乘法常数（⌊2^32/φ⌋）：相邻的 id 散到 [0, 2^32) 上极匀，按比例抽出的条数与「比例 × 池子」只差一两条
_QC_HASH = 2654435769


def _qc_picked(record_id: int, batch: str, ratio: float) -> bool:
    """这条随访在这个批次里抽不抽：只看（批次, 随访 id），与这次取到的池子里还有谁无关（P2-141）。

    同一批次重跑，池子里多出几条新办结的随访，原有的随访抽不抽不变、新来的照比例抽；换一个批次换一批人。
    """
    seed = zlib.crc32(batch.encode("utf-8"))
    return ((record_id + seed) * _QC_HASH) % 2**32 < ratio * 2**32


@router.post("/qc-samples/plan", response_model=QcPlanOut, dependencies=[Depends(require_roles("director", "doctor"))])
def plan_qc(
    body: QcPlanIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """按比例或数量制定抽查计划（智能随访端 #11）。

    按比例抽样按（批次, 随访 id）散列而不是随机：同一批次重复调用要抽到同一批人，
    否则质控员刷新一次页面，待抽查清单就换了一批。原先按「这次取到的清单里排第几」取模：
    清单按 id 倒序，两次点击之间每办结一条随访，位置整体后移，同一批次就换一批人、越抽越多；
    步长又取 `int(1 / 比例)`，比例 0.6 抽 100%、0.4 抽 50%（P2-141）。
    """
    query = db.query(SpdFollowupRecord).filter(SpdFollowupRecord.status == "done")
    orgs = visible_org_ids(db, user)
    if orgs is not None:
        query = query.filter(SpdFollowupRecord.org_id.in_(orgs))
    if body.dept:
        query = query.filter(SpdFollowupRecord.dept == body.dept)
    rows = query.order_by(SpdFollowupRecord.id.desc()).limit(2000).all()
    batch = body.batch or f"QC{clock.today().strftime('%Y%m%d')}"
    if body.count:
        picked = rows[: body.count]
    else:
        picked = [r for r in rows if _qc_picked(r.id, batch, body.ratio)]
    existing = {
        rid
        for (rid,) in db.query(SpdQcSample.record_id).filter(SpdQcSample.batch == batch).all()
    }
    created = 0
    # 上面的 existing 是快路径（重跑一次批次一条 SAVEPOINT 都不用开）；真正兜住并发的是
    # 唯一索引 uq_spd_qc_sample_record_batch——两个质控员同时点"生成抽查计划"，旧写法两路
    # 都读到空集、都插一遍，同一条随访在同一批次里被抽两次，合格率的分母直接翻倍。
    # 抢输的那行按顺序重跑的语义静默跳过（不计进 created），整批不因此回滚。
    # `picked` 必须保持 id.desc() 的顺序：所有请求按同一顺序取键锁，PG 上才不会互等成死锁。
    for record in picked:
        if record.id in existing:
            continue
        if insert_if_absent(
            db,
            SpdQcSample(
                record_id=record.id, batch=batch, dept=record.dept, sampler_id=user.id
            ),
        ):
            created += 1
    db.commit()
    return {"batch": batch, "pool": len(rows), "planned": len(picked), "created": created}


class QcResultIn(BaseModel):
    result: str = Field(pattern="^(pass|warn|fail)$")
    method: str = Field(default="record", pattern="^(record|phone|wechat)$")
    note: str = Field(default="", max_length=512)


@router.post("/qc-samples/{sample_id}/result", response_model=QcResultOut,
             dependencies=[Depends(require_roles("director", "doctor"))])
def record_qc_result(
    sample_id: int, body: QcResultIn, db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """记录质控抽查结论。

    归属隔着一跳：`spd_qc_samples` 没有机构列，经 `record_id` 回到
    `spd_followup_records.org_id`。

    ⚠️ **这不是给质控加了一道新口径，是把同一个功能的两半对齐。**
    同文件的 `plan_qc`（生成抽查计划）一直按 `visible_org_ids(db, user)` 收口——
    抽得到谁，本来就只有可见范围内那些；而判结论这一半**什么都不校验**，
    于是乙院的医师可以对甲院质控员抽出来的样本写"不合格"。
    合格率是考核依据，能被无关机构写进去，这个数就不能用了。

    用写档（`assert_org_writable`）而不是读档：记结论是**写**。
    今天两档对非全域角色宽窄相同，将来若分化（授权代录之类），
    这里该跟着写档走——见 `visibility.assert_org_writable` 的 docstring。
    全域角色（县级中心）照常跨机构判，那正是质控的本意。
    """
    sample = db.get(SpdQcSample, sample_id)
    if sample is None:
        raise HTTPException(status_code=404, detail="抽查记录不存在")
    record = db.get(SpdFollowupRecord, sample.record_id)
    assert_org_writable(db, user, record.org_id if record else None)
    sample.result = body.result
    sample.method = body.method
    sample.note = body.note
    db.commit()
    return {"id": sample.id, "result": sample.result}


@router.get("/qc-samples", response_model=list[QcSampleRowOut])
def list_qc_samples(
    response: Response, batch: str = "", result: str | None = None,
    offset: int = 0, limit: int = 100, db: Session = Depends(get_db),
):
    query = db.query(SpdQcSample)
    if batch:
        query = query.filter(SpdQcSample.batch == batch)
    if result:
        query = query.filter(SpdQcSample.result == result)
    rows = paginate(query.order_by(SpdQcSample.id.desc()), response, offset, limit)
    records = {
        r.id: r
        for r in db.query(SpdFollowupRecord)
        .filter(SpdFollowupRecord.id.in_([s.record_id for s in rows] or [0]))
    }
    return [
        {"id": s.id, "record_id": s.record_id, "batch": s.batch, "dept": s.dept,
         "result": s.result, "method": s.method, "note": s.note,
         "record": _record_out(records[s.record_id]) if s.record_id in records else None,
         "created_at": s.created_at.isoformat()}
        for s in rows
    ]


# ============================================================ 智能辅助 · 报告模板与推送


class ReportTemplateIn(BaseModel):
    code: str = Field(min_length=1, max_length=32, pattern=NON_BLANK)
    name: str = Field(min_length=1, max_length=64, pattern=NON_BLANK)
    period: str = Field(default="daily", pattern="^(daily|weekly|monthly|custom)$")
    scope_level: str = Field(default="center", pattern="^(center|dept|grassroots|personal)$")
    sections: list[dict] = Field(default_factory=list)
    variables: dict = Field(default_factory=dict)


def _template_out(t: SpdReportTemplate) -> dict:
    return {
        "id": t.id, "code": t.code, "name": t.name, "period": t.period,
        "scope_level": t.scope_level, "sections": t.sections or [],
        "variables": t.variables or {}, "active": t.active,
    }


@router.post("/report-templates", response_model=SpdReportTemplateOut, status_code=201,
             dependencies=[Depends(require_roles("director"))])
def create_report_template(body: ReportTemplateIn, db: Session = Depends(get_db)):
    if not body.sections:
        raise HTTPException(status_code=422, detail="报告模板至少要有一个内容段落")
    template = SpdReportTemplate(**body.model_dump())
    db.add(template)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该报告模板编码已存在") from None
    return _template_out(template)


@router.get("/report-templates", response_model=list[SpdReportTemplateOut])
def list_report_templates(
    response: Response,
    period: str | None = None,
    offset: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    query = db.query(SpdReportTemplate)
    if period:
        query = query.filter(SpdReportTemplate.period == period)
    return [
        _template_out(t)
        for t in paginate(query.order_by(SpdReportTemplate.id), response, offset, limit)
    ]


class ReportTemplatePatch(BaseModel):
    """改档与建档同一套约束（P1-94）：原先收裸 dict、照单全收。不传即不改；不可空的列显式传 null 是 422。"""

    name: str = Field(default=UNSET, min_length=1, max_length=64, pattern=NON_BLANK)
    period: str = Field(default=UNSET, pattern="^(daily|weekly|monthly|custom)$")
    scope_level: str = Field(default=UNSET, pattern="^(center|dept|grassroots|personal)$")
    sections: list[dict] = Field(default=UNSET)
    variables: dict = Field(default=UNSET)
    active: bool = Field(default=UNSET)


@router.patch("/report-templates/{template_id}", response_model=SpdReportTemplateOut,
              dependencies=[Depends(require_roles("director"))])
def update_report_template(template_id: int, body: ReportTemplatePatch, db: Session = Depends(get_db)):
    template = db.get(SpdReportTemplate, template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="报告模板不存在")
    changes = body.model_dump(exclude_unset=True)
    if "sections" in changes and not changes["sections"]:
        raise HTTPException(status_code=422, detail="报告模板至少要有一个内容段落")
    for key, value in changes.items():
        setattr(template, key, value)
    db.commit()
    return _template_out(template)


#: 推送时点 HH:MM，零补齐（P2-53）。调度按字符串比 `现在 HH:MM < push_time`：写成「8:00」「24:00」一天里
#: 任何时刻都比它小，任务永远跳过、也不报错。只收 ASCII 数字（全角「０８:００」同理比不对）。
PUSH_TIME_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


class ReportTaskIn(BaseModel):
    template_id: int
    name: str = Field(min_length=1, max_length=64, pattern=NON_BLANK)
    frequency: str = Field(default="daily", pattern="^(daily|weekly|monthly|custom)$")
    push_time: str = Field(default="08:00", pattern=PUSH_TIME_PATTERN)
    subscriber_ids: list[int] = Field(default_factory=list)
    org_ids: list[int] = Field(default_factory=list)
    valid_from: OptionalDateStr = ""
    valid_to: OptionalDateStr = ""
    priority: int = Field(default=1, ge=1, le=9)


def _check_report_refs(db: Session, subscriber_ids: list[int], org_ids: list[int], *,
                       old_subscribers: list[int] | None = None, old_orgs: list[int] | None = None) -> None:
    """订阅人与推送机构写库之前先查（P1-124）：两者都是 JSON 列表、库里没有外键，可到点推送时要写进带外键的列——
    报告实例的机构、站内消息的收件人。原先照单全收，填错一个编号，定时推送每一轮都撞约束、整轮回滚，所有任务的报告都
    不出。订阅人与其余挂人的字段同一口径（P1-106）：不存在、已停用的都不收。改档时原有的编号不再查（已停用的订阅人
    不该挡住改推送时点）。"""
    for user_id in dict.fromkeys(subscriber_ids):
        if user_id in (old_subscribers or []):
            continue
        state = unusable_user(db, user_id)
        if state:
            raise HTTPException(status_code=404, detail=f"订阅人{state}（user_id={user_id}）")
    org_problem = unknown_ids(db, Organization, org_ids, "机构", already=old_orgs)
    if org_problem:
        raise HTTPException(status_code=404, detail=org_problem)


def _task_out(t: SpdReportTask) -> dict:
    return {
        "id": t.id, "template_id": t.template_id, "name": t.name,
        "frequency": t.frequency, "push_time": t.push_time,
        "subscriber_ids": t.subscriber_ids or [], "org_ids": t.org_ids or [],
        "valid_from": t.valid_from, "valid_to": t.valid_to, "priority": t.priority,
        "status": t.status,
        "last_run_at": t.last_run_at.isoformat() if t.last_run_at else "",
    }


def _check_valid_window(valid_from: str, valid_to: str) -> None:
    """有效期止不得早于起（区间起止顺序）：调度按 `今天 < 起` 或 `今天 > 止` 跳过，倒置的有效期让它天天跳过——
    任务照样显示「启用中」，一份报告也不生成（2026-09-24 实测修前 201，冻结在三个日期各跑一轮都是 0 份）。"""
    if valid_from and valid_to and valid_to < valid_from:
        raise HTTPException(status_code=422, detail="有效期止不得早于有效期起")


@router.post("/report-tasks", response_model=ReportTaskOut, status_code=201,
             dependencies=[Depends(require_roles("director"))])
def create_report_task(body: ReportTaskIn, db: Session = Depends(get_db)):
    # 停用的模板不收：调度见模板停用就跳过（jobs.spd_report_push），建在它上面的任务显示「启用」、
    # 一份报告也不出——与有效期倒置同一种「照样 201、之后天天被跳过」（2026-09-24 实测修前 201，
    # 到点调度生成 0 份、last_run_at 一直是空的）。手动「生成报告」不在此列：当场出结果，看得见。
    template = db.get(SpdReportTemplate, body.template_id)
    if template is None or not template.active:
        raise HTTPException(status_code=404, detail="报告模板不存在或已停用")
    _check_valid_window(body.valid_from, body.valid_to)
    _check_report_refs(db, body.subscriber_ids, body.org_ids)
    task = SpdReportTask(**body.model_dump())
    db.add(task)
    db.commit()
    return _task_out(task)


@router.get("/report-tasks", response_model=list[ReportTaskOut])
def list_report_tasks(status: str | None = None, db: Session = Depends(get_db)):
    query = db.query(SpdReportTask)
    if status:
        query = query.filter(SpdReportTask.status == status)
    return [
        _task_out(t)
        for t in query.order_by(SpdReportTask.priority, SpdReportTask.id).limit(200).all()
    ]


class ReportTaskPatch(BaseModel):
    """改档与建档同一套约束（P1-94）：原先收裸 dict、照单全收。不传即不改；不可空的列显式传 null 是 422。"""

    name: str = Field(default=UNSET, min_length=1, max_length=64, pattern=NON_BLANK)
    frequency: str = Field(default=UNSET, pattern="^(daily|weekly|monthly|custom)$")
    push_time: str = Field(default=UNSET, pattern=PUSH_TIME_PATTERN)
    subscriber_ids: list[int] = Field(default=UNSET)
    org_ids: list[int] = Field(default=UNSET)
    valid_from: OptionalDateStr = Field(default=UNSET)
    valid_to: OptionalDateStr = Field(default=UNSET)
    priority: int = Field(default=UNSET, ge=1, le=9)
    status: str = Field(default=UNSET, pattern="^(active|paused|deleted)$")


@router.patch("/report-tasks/{task_id}", response_model=ReportTaskOut, dependencies=[Depends(require_roles("director"))])
def update_report_task(task_id: int, body: ReportTaskPatch, db: Session = Depends(get_db)):
    """启用 / 暂停 / 改频率 / 调优先级。删除也走这里（status=deleted 由前端不再展示）。"""
    task = db.get(SpdReportTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="报告推送任务不存在")
    changes = body.model_dump(exclude_unset=True)
    # 起止与建档同一句，与存量合并后再比；只在这次改了起止时查——存量里已倒置的任务，暂停 / 删除它不该被拦
    if {"valid_from", "valid_to"} & changes.keys():
        _check_valid_window(changes.get("valid_from", task.valid_from), changes.get("valid_to", task.valid_to))
    _check_report_refs(db, changes.get("subscriber_ids", []), changes.get("org_ids", []),
                       old_subscribers=task.subscriber_ids, old_orgs=task.org_ids)
    for key, value in changes.items():
        setattr(task, key, value)
    db.commit()
    return _task_out(task)


@router.delete("/report-tasks/{task_id}", status_code=204,
               dependencies=[Depends(require_roles("director"))])
def delete_report_task(task_id: int, db: Session = Depends(get_db)):
    task = db.get(SpdReportTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="报告推送任务不存在")
    # 生成过报告的任务不删（P2-59）：报告实例按 task_id 挂在它上面。真 PG 上撞外键即 500；开发库照删，
    # 实例留着悬空的 task_id，下一个新建的任务还会复用这个编号，把旧报告认成自己的
    if db.query(SpdReportInstance.id).filter(SpdReportInstance.task_id == task_id).first() is not None:
        raise HTTPException(status_code=409, detail="该任务已生成过报告，不能删除；不再推送请改为暂停")
    db.delete(task)
    db.commit()
    return Response(status_code=204)


class GenerateReportIn(BaseModel):
    task_id: int | None = None
    template_code: str = Field(default="", max_length=32)
    org_id: int | None = None
    period_label: str = Field(default="", max_length=32)


@router.post("/report-instances", response_model=ReportGeneratedOut, status_code=201,
             dependencies=[Depends(require_roles("director", "doctor"))])
def generate_report(
    body: GenerateReportIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """按模板生成一份报告实例（智能辅助端 #1/#3）。

    段落内容经 `spd/reporting.py` 的注册表从本平台各业务表实时聚合——报告不是
    另存一份统计结果，而是**同一批数字的另一种排版**；指标段落（key=indicator）
    直接复用考核指标库的取数与公式，从结构上保证报表与考核同源。
    """
    task = db.get(SpdReportTask, body.task_id) if body.task_id is not None else None
    if body.task_id is not None and task is None:
        raise HTTPException(status_code=404, detail="报告推送任务不存在")
    template = (
        db.get(SpdReportTemplate, task.template_id) if task is not None
        else db.query(SpdReportTemplate)
        .filter(SpdReportTemplate.code == body.template_code).first()
    )
    if template is None:
        raise HTTPException(status_code=404, detail="报告模板不存在")

    org_id = body.org_id if body.org_id is not None else user.org_id
    assert_org_writable(db, user, org_id)  # P0-35：报告实例挂在这家机构名下，只能以本机构名义生成
    period_label = body.period_label or default_period_label(template.period)
    content = {
        "period_label": period_label,
        "sections": [
            compose_section(db, section, org_id, template.period)
            for section in template.sections or []
        ],
    }
    instance = SpdReportInstance(
        task_id=task.id if task else None, template_code=template.code,
        title=f"{template.name}（{period_label}）", period_label=period_label,
        scope_level=template.scope_level, org_id=org_id, content=content,
        subscriber_ids=(task.subscriber_ids if task else []) or [],
    )
    db.add(instance)
    if task is not None:
        task.last_run_at = now_naive()
    db.commit()
    return {
        "id": instance.id, "title": instance.title, "period_label": period_label,
        "content": content,
    }


@router.get("/report-instances", response_model=list[ReportInstanceRowOut])
def list_report_instances(
    response: Response,
    template_code: str | None = None,
    mine: bool = False,
    offset: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = db.query(SpdReportInstance)
    if template_code:
        query = query.filter(SpdReportInstance.template_code == template_code)
    rows = paginate(query.order_by(SpdReportInstance.id.desc()), response, offset, limit)
    if mine:
        rows = [
            r for r in rows
            if not (r.subscriber_ids or []) or user.id in (r.subscriber_ids or [])
        ]
    return [
        {"id": r.id, "title": r.title, "template_code": r.template_code,
         "period_label": r.period_label, "scope_level": r.scope_level,
         "org_id": r.org_id, "created_at": r.created_at.isoformat()}
        for r in rows
    ]


@router.get("/report-instances/{instance_id}", response_model=ReportInstanceDetailOut)
def get_report_instance(instance_id: int, db: Session = Depends(get_db)):
    instance = db.get(SpdReportInstance, instance_id)
    if instance is None:
        raise HTTPException(status_code=404, detail="报告不存在")
    return {
        "id": instance.id, "title": instance.title,
        "template_code": instance.template_code, "period_label": instance.period_label,
        "scope_level": instance.scope_level, "org_id": instance.org_id,
        "content": instance.content or {},
        "subscriber_ids": instance.subscriber_ids or [],
        "created_at": instance.created_at.isoformat(),
    }


@router.get("/health-calendar", response_model=HealthCalendarOut)
def health_calendar(
    patient_id: int, day: str = "", db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """患者健康日历（智能随访端 #12）：某天的随访、宣教与复诊安排。"""
    assert_patient_visible(db, user, patient_id, resource="spd_calendar")
    # `day` 是按字符串等值匹配的：`2026-9-24` 会让这一天"什么安排都没有"，
    # 而页面上那个输入框是自由文本（P1-58）。
    target = require_date(day, field="day") if day else clock.today().isoformat()
    followups = (
        db.query(SpdFollowupRecord)
        .filter(
            SpdFollowupRecord.patient_id == patient_id,
            SpdFollowupRecord.planned_at == target,
        )
        .all()
    )
    revisits = (
        db.query(SpdRevisit)
        .filter(SpdRevisit.patient_id == patient_id, SpdRevisit.plan_date == target)
        .all()
    )
    tasks = (
        db.query(SpdTask)
        .filter(SpdTask.patient_id == patient_id, SpdTask.due_date == target)
        .all()
    )
    return {
        "day": target,
        "followups": [_record_out(f) for f in followups],
        "revisits": [
            {"id": r.id, "plan_date": r.plan_date, "dept": r.dept, "items": r.items,
             "status": r.status}
            for r in revisits
        ],
        "tasks": [
            {"id": t.id, "title": t.title, "task_type": t.task_type, "status": t.status}
            for t in tasks
        ],
    }
