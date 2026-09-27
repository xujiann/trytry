"""集中审方中心："系统+药师"双重审方，每方必审。"""
from datetime import date
from typing import Any, cast

from pydantic import BaseModel, Field
from sqlalchemy import func, update
from sqlalchemy.engine import CursorResult

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from .. import clock
from ..concurrency import appended_text, insert_if_absent, insert_or_conflict
from ..database import get_db
from ..deps import get_current_user, paginate, require_admin, require_roles
from ..models import (
    DrugRule,
    DrugRuleChange,
    MaternalRecord,
    Organization,
    Patient,
    Prescription,
    PrescriptionComment,
    PrescriptionItem,
    User,
)
from ..texttypes import split_list
from ..visibility import assert_org_writable
from ..schemas import (
    SPECIAL_GROUP_NAMES,
    DrugRuleCreate,
    DrugRuleOut,
    PrescriptionCreate,
    PrescriptionOut,
    REVIEW_COMMENT_MAX,
    PrescriptionReview,
)

router = APIRouter(prefix="/api/prescriptions", tags=["集中审方"])

# 状态文案（措辞照抄模型列注释；报错文案用它，别把英文码直接拼给窗口人员看——P2-74）。处方笺打印件也用这一张，
# 与处方列表页（core.js RX_STATUS）逐字相同（P2-575，test_exam_rx_status_one_voice 盯着）
PRESCRIPTION_STATUS_NAMES = {"auto_passed": "系统审通过", "pending_review": "待药师审", "approved": "药师审通过", "rejected": "已退回"}

# 特殊人群年龄界限：儿童 <14 岁，老年 ≥65 岁
CHILD_AGE_LIMIT = 14
ELDERLY_AGE_LIMIT = 65

GROUP_NAMES = SPECIAL_GROUP_NAMES


def _age_of(birth_date: str, today: date | None = None) -> int | None:
    """按出生日期（YYYY-MM-DD）计算周岁；无法解析返回 None。"""
    try:
        born = date.fromisoformat(birth_date)
    except (TypeError, ValueError):
        return None
    ref = today or clock.today()
    return ref.year - born.year - ((ref.month, ref.day) < (born.month, born.day))


def _patient_groups(db: Session, patient: Patient) -> set[str]:
    """推断患者所属特殊人群：儿童/老年按 birth_date，孕产妇按在册孕产记录+性别。

    「在册」= 未结案：孕期（registered）与已分娩、产后访视还没结案（delivered，产褥期 / 哺乳期）都算孕产妇。
    原先只认孕期（P2-120）：刚分娩的产妇开他汀、利伐沙班照样系统审通过，而这两味的说明书哺乳期同样禁用
    （ACEI / ARB 哺乳期也要权衡）——规则把它们挂在孕产妇上，要的就是药师看一眼。
    性别只排除明确登记为「男」的（P1-153）：网页建档的性别缺省「未知」，孕产妇建册又不要求性别是女——原先要求
    性别等于「女」，在册孕妇的档案性别没改过就永远不算孕产妇，致畸药照样系统审通过。在册的孕产档案本身就是证据。
    """
    groups: set[str] = set()
    age = _age_of(patient.birth_date)
    if age is not None:
        if age < CHILD_AGE_LIMIT:
            groups.add("child")
        if age >= ELDERLY_AGE_LIMIT:
            groups.add("elderly")
    if patient.gender != "男":
        maternal = (
            db.query(MaternalRecord)
            .filter(MaternalRecord.patient_id == patient.id, MaternalRecord.status != "closed")
            .first()
        )
        if maternal is not None:
            groups.add("pregnant")
    return groups


def _active_rule(db: Session, drug_code: str) -> DrugRule | None:
    """取生效中的规则。停用的规则一律当作"未维护"，与规则不存在同路处理。"""
    return (
        db.query(DrugRule)
        .filter(DrugRule.drug_code == drug_code, DrugRule.active.is_(True))
        .first()
    )


def _rule_snapshot(rule: DrugRule | None) -> dict:
    """开方那一刻系统审用的规则参数，记在处方明细上（P2-577）。没有生效规则的不记（全空），判读时照旧按现行规则。"""
    if rule is None:
        return {}
    return {"rule_max_daily_dose": rule.max_daily_dose, "rule_dose_unit": rule.dose_unit,
            "rule_antibiotic": rule.antibiotic, "rule_ddd": rule.ddd}


#: 规则改动记录的动作（P2-578；措辞照抄模型列注释）
RULE_CHANGE_ACTION_NAMES = {"create": "新建", "import": "导入", "deactivate": "停用", "reactivate": "恢复"}
#: 改动记录里逐项列出的字段与中文名（DrugRuleCreate 的字段加生效标记）
RULE_FIELD_NAMES = {
    "drug_code": "药品编码", "max_daily_dose": "日剂量上限", "dose_unit": "剂量单位", "note": "备注",
    "interactions": "相互作用", "contraindicated_diagnoses": "禁忌诊断", "special_groups": "特殊人群",
    "renal_hepatic_note": "肝肾功能提示", "review_points": "点评要点", "antibiotic": "抗菌药物", "ddd": "DDD",
    "active": "生效",
}


def _rule_state(rule: DrugRule) -> dict:
    """整条规则此刻的值：改动记录的前后快照。"""
    return {field: getattr(rule, field) for field in RULE_FIELD_NAMES}


def _log_rule_change(db: Session, action: str, before: dict | None, rule: DrugRule, user: User) -> None:
    """记一条规则改动（P2-578）。前后一模一样的不记：导入同样的值、恢复本就生效的，都不算改过。"""
    after = _rule_state(rule)
    if before == after:
        return
    db.add(DrugRuleChange(drug_code=rule.drug_code, action=action, before=before, after=after,
                          changed_by=user.id))


@router.post("/rules", response_model=DrugRuleOut, status_code=201, dependencies=[Depends(require_admin)])
def create_rule(body: DrugRuleCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if db.query(DrugRule).filter(DrugRule.drug_code == body.drug_code).first():
        raise HTTPException(status_code=409, detail="该药品规则已存在")
    rule = DrugRule(**body.model_dump(), active=True)
    _log_rule_change(db, "create", None, rule, user)   # 与规则同一次提交：撞了唯一约束一并回滚
    return insert_or_conflict(db, rule, "该药品规则已存在")


class RuleImportOut(BaseModel):
    imported: int
    updated: int


class RuleActiveOut(BaseModel):
    """停用/恢复回执同形：编码 + 最新生效位。"""

    drug_code: str
    active: bool


@router.post("/rules/import", response_model=RuleImportOut, dependencies=[Depends(require_admin)])
def import_rules(body: list[DrugRuleCreate], db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """审方规则批量导入：drug_code 已存在则整条更新，不存在则新建。每条新建与覆盖都记改动前后（P2-578）。"""
    imported, updated = 0, 0
    for entry in body:
        rule = db.query(DrugRule).filter(DrugRule.drug_code == entry.drug_code).first()
        # 先试插；撞了说明有人并发导入了同一个 drug_code，取回来按更新处理。
        # 反过来"查不到就插"是 check-then-act，而这里一次 commit 提交整批，
        # 一条撞车整批回滚——导入方看到的是 500 与一条都没进。
        fresh = DrugRule(**entry.model_dump(), active=True)
        if rule is None and insert_if_absent(db, fresh):
            _log_rule_change(db, "import", None, fresh, user)
            imported += 1
            continue
        if rule is None:
            rule = db.query(DrugRule).filter(DrugRule.drug_code == entry.drug_code).first()
            if rule is None:  # pragma: no cover - 撞了约束却查不到，说明约束定义有误
                continue
        before = _rule_state(rule)
        for field, value in entry.model_dump().items():
            setattr(rule, field, value)
        _log_rule_change(db, "import", before, rule, user)
        updated += 1
    db.commit()
    return {"imported": imported, "updated": updated}


@router.get("/rules", response_model=list[DrugRuleOut], dependencies=[Depends(get_current_user)])
def list_rules(include_inactive: bool = False, db: Session = Depends(get_db)):
    query = db.query(DrugRule)
    if not include_inactive:
        query = query.filter(DrugRule.active.is_(True))
    return query.order_by(DrugRule.drug_code).all()


@router.delete(
    "/rules/{drug_code}", response_model=RuleActiveOut, dependencies=[Depends(require_admin)]
)
def deactivate_rule(drug_code: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """停用规则（不删行）。

    原先这里只有 POST 与 import，录错一条规则只能靠 import 覆盖同 drug_code
    的行，删不掉也停不掉——而通用规则引擎 `/api/rules/{key}` 一直是有停用的。
    不删行：规则改过什么、什么时候不再生效，处方点评复核时要回溯得到（何时停用、谁停的记进改动记录，P2-578）。
    """
    rule = db.query(DrugRule).filter(DrugRule.drug_code == drug_code).first()
    if rule is None:
        raise HTTPException(status_code=404, detail="规则不存在")
    if not rule.active:
        raise HTTPException(status_code=409, detail="该规则已停用")
    before = _rule_state(rule)
    rule.active = False
    _log_rule_change(db, "deactivate", before, rule, user)
    db.commit()
    return {"drug_code": drug_code, "active": False}


@router.post(
    "/rules/{drug_code}/reactivate",
    response_model=RuleActiveOut,
    dependencies=[Depends(require_admin)],
)
def reactivate_rule(drug_code: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rule = db.query(DrugRule).filter(DrugRule.drug_code == drug_code).first()
    if rule is None:
        raise HTTPException(status_code=404, detail="规则不存在")
    before = _rule_state(rule)
    rule.active = True
    _log_rule_change(db, "reactivate", before, rule, user)
    db.commit()
    return {"drug_code": drug_code, "active": True}


class RuleFieldChangeOut(BaseModel):
    """改动记录里的一项：字段中文名与改前 / 改后的显示值（新建的改前为「—」）。"""

    field: str
    label: str
    before: str
    after: str


class DrugRuleChangeOut(BaseModel):
    id: int
    drug_code: str
    action: str
    action_name: str
    #: 改了哪几项（新建列全部字段）
    changes: list[RuleFieldChangeOut]
    #: 改动人姓名（没填姓名的退回账号；账号已删的为空串）
    changed_by: str
    #: 改动时刻：带偏移的本地时间（给人看的，页面取前 19 位）
    at: str


def _shown(value: Any) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    return str(value)


@router.get("/rules/{drug_code}/changes", response_model=list[DrugRuleChangeOut],
            dependencies=[Depends(get_current_user)])
def list_rule_changes(drug_code: str, response: Response, offset: int = 0, limit: int = 200,
                      db: Session = Depends(get_db)):
    """一条审方规则的改动记录（P2-578），最新的在前：每次新建、导入覆盖、停用、恢复改了哪几项、改前改后、谁、何时。

    与规则清单同一个可见范围（登录即可看）。改动记录从本表上线起才有：上线前的改动无从得知。
    """
    if db.query(DrugRule.id).filter(DrugRule.drug_code == drug_code).first() is None:
        raise HTTPException(status_code=404, detail="规则不存在")
    rows = paginate(
        db.query(DrugRuleChange).filter(DrugRuleChange.drug_code == drug_code).order_by(DrugRuleChange.id.desc()),
        response, offset, limit,
    )
    user_ids = {r.changed_by for r in rows if r.changed_by is not None}
    names = {u.id: u.full_name or u.username for u in db.query(User).filter(User.id.in_(user_ids))} if user_ids else {}
    out = []
    for r in rows:
        before = r.before or {}
        changes = [
            {"field": field, "label": label, "before": _shown(before.get(field)) if r.before is not None else "—",
             "after": _shown(r.after.get(field))}
            for field, label in RULE_FIELD_NAMES.items()
            if r.before is None or before.get(field) != r.after.get(field)
        ]
        out.append({
            "id": r.id, "drug_code": r.drug_code, "action": r.action,
            "action_name": RULE_CHANGE_ACTION_NAMES.get(r.action, r.action), "changes": changes,
            "changed_by": names.get(r.changed_by, "") if r.changed_by is not None else "",
            "at": clock.local_iso(r.created_at),
        })
    return out


@router.post(
    "",
    response_model=PrescriptionOut,
    status_code=201,
    dependencies=[Depends(require_roles("doctor"))],  # H2: 处方开具限医师
)
def create_prescription(
    body: PrescriptionCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # P0-35：开方机构由请求声明——乙院医生以甲院名义开的处方照样进甲院审方、自动通过（实测 201）。
    assert_org_writable(db, user, body.org_id)
    patient = db.get(Patient, body.patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")

    violations: list[str] = []

    # 同方重复药品编码 → 转药师审
    codes = [item.drug_code for item in body.items]
    duplicated = sorted({c for c in codes if codes.count(c) > 1})
    for code in duplicated:
        names = {item.drug_name for item in body.items if item.drug_code == code}
        violations.append(f"同方重复药品：{'/'.join(sorted(names))}（{code}）出现多次，需药师人工审核")

    advisories: list[str] = []
    patient_groups = _patient_groups(db, patient)
    names_by_code = {item.drug_code: item.drug_name for item in body.items}
    seen_pairs: set[frozenset[str]] = set()
    # 审方用的这一版规则，同一版随明细落库（P2-577）：之后规则再改，这张方怎么审的、按什么单位开的都回溯得到
    rules = {code: _active_rule(db, code) for code in dict.fromkeys(codes)}
    for item in body.items:
        rule = rules[item.drug_code]
        if rule is None:
            continue
        if item.daily_dose > rule.max_daily_dose:
            violations.append(
                f"{item.drug_name} 日剂量 {item.daily_dose}{rule.dose_unit} 超过上限 "
                f"{rule.max_daily_dose}{rule.dose_unit}"
            )
        # 相互作用审查：同一处方内出现冲突药对 → 转药师审并注明
        conflict_codes = set(split_list(rule.interactions))
        for other_code in conflict_codes & set(names_by_code) - {item.drug_code}:
            pair = frozenset((item.drug_code, other_code))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            violations.append(
                f"药物相互作用：{item.drug_name} 与 {names_by_code[other_code]} 存在相互作用，需药师人工审核"
            )
        # 禁忌诊断审查：诊断名命中禁忌关键词 → 转药师审并注明
        # 清单按半角 / 全角逗号、顿号拆（P1-137）：导入 JSON 里写「妊娠，哺乳期」原先是一个词，这条禁忌从不触发
        for keyword in split_list(rule.contraindicated_diagnoses):
            if keyword in body.diagnosis_name:
                violations.append(
                    f"禁忌诊断：{item.drug_name} 禁用于「{keyword}」相关诊断"
                    f"（本方诊断：{body.diagnosis_name}），需药师人工审核"
                )
        # 特殊人群审查：患者命中规则特殊人群 → 转药师审并注明
        rule_groups = set(split_list(rule.special_groups))
        for group in sorted(rule_groups & patient_groups):
            violations.append(
                f"特殊人群用药：{item.drug_name} 对{GROUP_NAMES.get(group, group)}需慎用，需药师人工审核"
            )
        # 块2：肝肾功能提示为非拦截性提醒，随处方返回供医师调整剂量，不改变审方状态
        if rule.renal_hepatic_note:
            note = f"{item.drug_name} 肝肾功能提示：{rule.renal_hepatic_note}"
            if note not in advisories:
                advisories.append(note)

    prescription = Prescription(
        patient_id=body.patient_id,
        org_id=body.org_id,
        diagnosis_name=body.diagnosis_name,
        status="pending_review" if violations else "auto_passed",
        review_comment=_system_review_comment(violations),
        created_by=user.id,
    )
    db.add(prescription)
    db.flush()
    for item in body.items:
        db.add(PrescriptionItem(prescription_id=prescription.id, **item.model_dump(),
                                **_rule_snapshot(rules[item.drug_code])))
    db.commit()
    db.refresh(prescription)
    # 非持久化字段：审方提示只随本次响应返回、不入库（`PrescriptionOut.advisories`）。
    # 用 setattr 挂上去——模型上没有这一列，写成属性赋值等于对 ORM 撒谎。
    setattr(prescription, "advisories", advisories)
    return prescription


@router.get("", response_model=list[PrescriptionOut], dependencies=[Depends(get_current_user)])
def list_prescriptions(
    response: Response,
    status: str | None = None,
    offset: int = 0,
    limit: int = 200,
    db: Session = Depends(get_db),
):
    """处方列表（L-3 分页：offset/limit，总数见 X-Total-Count 响应头）。"""
    query = db.query(Prescription)
    if status:
        query = query.filter(Prescription.status == status)
    return paginate(query.order_by(Prescription.id.desc()), response, offset, limit)


@router.post(
    "/{prescription_id}/review",
    response_model=PrescriptionOut,
    dependencies=[Depends(require_roles("pharmacist"))],
)
def review_prescription(prescription_id: int, body: PrescriptionReview, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    prescription = db.get(Prescription, prescription_id)
    if prescription is None:
        raise HTTPException(status_code=404, detail="处方不存在")
    # 修前建的处方，系统审方意见可能已占满大半列（P2-229）：意见装不下就说清还能写几个字，别让生产库 500。
    # 待审处方的系统意见只在建方时写一次，这里读到的就是审核那条 UPDATE 要接在后面的那一段
    existing = prescription.review_comment or ""
    room = REVIEW_COLUMN_MAX - len(existing) - (1 if existing else 0) - len(REVIEWER_PREFIX)
    if body.comment and len(body.comment) > room:
        raise HTTPException(status_code=422, detail=f"药师意见最多还能写 {max(room, 0)} 字（这张处方的系统审方意见已占 {len(existing)} 字）")
    if not _apply_review(db, prescription_id, "approved" if body.approve else "rejected", body.comment):
        db.rollback()
        db.refresh(prescription)  # 抢输了就按真实状态措辞，别拿锁外读到的旧值
        raise HTTPException(status_code=409, detail=f"当前状态 {PRESCRIPTION_STATUS_NAMES.get(prescription.status, prescription.status)} 无需药师审核")
    db.commit()
    db.refresh(prescription)
    return prescription


#: `prescriptions.review_comment` 的列长（模型 `String(1024)`，用例钉着两边同一个数）：系统审方意见与药师意见拼在这一列里（P2-229）
REVIEW_COLUMN_MAX = 1024
#: 药师意见接在系统意见后面的前缀（分隔符「；」另算）
REVIEWER_PREFIX = "药师意见："
#: 系统审方意见的上限：给药师意见留足位置——`系统意见；药师意见：<至多 REVIEW_COMMENT_MAX 字>` 装得进这一列
SYSTEM_REVIEW_MAX = REVIEW_COLUMN_MAX - len("；" + REVIEWER_PREFIX) - REVIEW_COMMENT_MAX


def _system_review_comment(violations: list[str]) -> str:
    """系统审方意见：逐条用「；」连起来；超出上限的截断并注明共几条（P2-229）。

    原先整串照写：命中的禁忌 / 相互作用 / 特殊人群一多（每条都带着药名，禁忌诊断还带着整段诊断名），拼出来超过
    列长 1024，生产库建处方即 500——医生连方都开不出去。审方看的是「为什么转人工」，列出前面的并注明总数不误判断，
    明细随时能按处方明细与用药规则重算。上限还给药师意见留了位置（`SYSTEM_REVIEW_MAX`）。"""
    text = "；".join(violations)
    if len(text) <= SYSTEM_REVIEW_MAX:
        return text
    marker = f"……（共 {len(violations)} 条，余下从略）"
    return text[: SYSTEM_REVIEW_MAX - len(marker)] + marker


def _apply_review(db: Session, prescription_id: int, status: str, comment: str) -> bool:
    """药师审核落库：状态迁移与意见追加压在**同一条带状态条件的 UPDATE** 里，返回是否审到。

    旧写法是"读状态 → 判 pending_review → 改对象 → commit"：两位药师同时点审核，
    都读到 pending_review，八路并发八路全过——结论以最后提交的为准（通过被盖成退回，
    或反过来），意见串也是读-改-写、只剩最后一位的。`WHERE status = 'pending_review'`
    让后到的那几路 rowcount 为 0，与顺序请求一样拿 409：一张处方只被审一次，
    意见追加在系统审意见之后（空则不带分隔符，同旧写法）。
    """
    values: dict[str, Any] = {"status": status}
    if comment:
        values["review_comment"] = appended_text(Prescription.review_comment, f"{REVIEWER_PREFIX}{comment}")
    reviewed = cast(CursorResult, db.execute(
        update(Prescription)
        .where(Prescription.id == prescription_id, Prescription.status == "pending_review")
        .values(**values)
    ))
    return bool(reviewed.rowcount)


# ---------- 终审轮：处方点评（⑱事后点评与监管） ----------


class RxCommentCreate(BaseModel):
    grade: str = Field(pattern="^(reasonable|unreasonable)$")
    issues: str = Field(default="", max_length=256)
    comment: str = Field(default="", max_length=1024)


class ReviewPointItemOut(BaseModel):
    """逐药点评要点行。`daily_dose`/`max_daily_dose` 是 **Float 列**
    （`prescription_items.daily_dose` / `drug_rules.max_daily_dose`）：整数入参 4
    落库读回就是 4.0，声明 float 才是原样——与 Money 列相反。
    无规则时上限为 null（键恒在值可空），单位与要点回落空串。"""

    drug_code: str
    drug_name: str
    daily_dose: float
    max_daily_dose: float | None
    dose_unit: str
    dose_exceeded: bool
    review_points: str
    renal_hepatic_note: str
    no_rule: bool


class ReviewPointsOut(BaseModel):
    prescription_id: int
    diagnosis_name: str
    status: str
    system_review_comment: str
    items: list[ReviewPointItemOut]
    # 两条产地（round(x*100.0/n, 2) 与空方兜底字面量 0.0）都是浮点
    rule_coverage_pct: float


@router.get(
    "/{prescription_id}/review-points",
    response_model=ReviewPointsOut,
    dependencies=[Depends(get_current_user)],
)
def prescription_review_points(prescription_id: int, db: Session = Depends(get_db)):
    """块2：处方点评规则化——按处方内药品汇总规则库点评要点与肝肾功能提示。

    药师点评前调阅本接口，把「凭经验点评」变为「按规则点评」：
    - review_points：该药的点评要点（剂量/疗程/联用/特殊人群等核对项）
    - renal_hepatic_note：肝肾功能剂量调整提示
    - dose_exceeded：本方日剂量是否已超规则上限（系统审拦截项复核）
    - no_rule：规则库中无该药规则，提示补充维护

    上限、单位、超没超按**开方那一刻**审方用的那一版判读（明细上的快照，P2-577）：规则之后纠正单位、收紧上限、停用，
    都不改写这张方当时怎么审的；点评要点与肝肾提示是给点评人看的文字，取规则行现在的写法（停用了行也还在）。
    没有快照的（开方时该药没有生效规则，或是快照列上线前开的）照旧按现行生效规则。
    """
    rx = db.get(Prescription, prescription_id)
    if rx is None:
        raise HTTPException(status_code=404, detail="处方不存在")
    items = db.query(PrescriptionItem).filter(PrescriptionItem.prescription_id == rx.id).all()
    points, uncovered = [], 0
    for item in items:
        max_dose: float | None
        if item.rule_max_daily_dose is not None:
            text_rule = db.query(DrugRule).filter(DrugRule.drug_code == item.drug_code).first()
            max_dose, unit = item.rule_max_daily_dose, item.rule_dose_unit
        else:
            text_rule = _active_rule(db, item.drug_code)
            max_dose, unit = (text_rule.max_daily_dose, text_rule.dose_unit) if text_rule else (None, "")
        if max_dose is None:
            uncovered += 1
        points.append(
            {
                "drug_code": item.drug_code,
                "drug_name": item.drug_name,
                "daily_dose": item.daily_dose,
                "max_daily_dose": max_dose,
                "dose_unit": unit,
                "dose_exceeded": max_dose is not None and item.daily_dose > max_dose,
                "review_points": text_rule.review_points if text_rule else "",
                "renal_hepatic_note": text_rule.renal_hepatic_note if text_rule else "",
                "no_rule": max_dose is None,
            }
        )
    return {
        "prescription_id": rx.id,
        "diagnosis_name": rx.diagnosis_name,
        "status": rx.status,
        "system_review_comment": rx.review_comment,
        "items": points,
        "rule_coverage_pct": round((len(items) - uncovered) * 100.0 / len(items), 2) if items else 0.0,
    }


class RxCommentCreatedOut(BaseModel):
    id: int
    prescription_id: int
    grade: str


class RxCommentOut(BaseModel):
    id: int
    prescription_id: int
    grade: str
    issues: str
    comment: str
    at: str


class RxCommentStatsOut(BaseModel):
    commented: int
    unreasonable: int
    # 两条产地（真除法 *100.0 与零点评兜底字面量 0.0）都是浮点
    reasonable_rate_pct: float


@router.post(
    "/{prescription_id}/comment-review",
    response_model=RxCommentCreatedOut,
    status_code=201,
    dependencies=[Depends(require_roles("pharmacist"))],  # 处方点评=药师
)
def comment_prescription(
    prescription_id: int,
    body: RxCommentCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    rx = db.get(Prescription, prescription_id)
    if rx is None:
        raise HTTPException(status_code=404, detail="处方不存在")
    if db.query(PrescriptionComment).filter(
        PrescriptionComment.prescription_id == prescription_id
    ).first():
        raise HTTPException(status_code=409, detail="该处方已点评")
    if body.grade == "unreasonable" and not (body.issues or body.comment):
        raise HTTPException(status_code=422, detail="不合理处方须注明问题类型或点评意见")
    record = insert_or_conflict(db, PrescriptionComment(
            prescription_id=prescription_id, reviewer_id=user.id, **body.model_dump()
        ), "该处方已点评")
    return {"id": record.id, "prescription_id": prescription_id, "grade": record.grade}


@router.get(
    "/comment-reviews",
    response_model=list[RxCommentOut],
    dependencies=[Depends(get_current_user)],
)
def list_comment_reviews(grade: str | None = None, db: Session = Depends(get_db)):
    q = db.query(PrescriptionComment)
    if grade:
        q = q.filter(PrescriptionComment.grade == grade)
    return [
        {
            "id": c.id,
            "prescription_id": c.prescription_id,
            "grade": c.grade,
            "issues": c.issues,
            "comment": c.comment,
            "at": c.created_at.isoformat(),
        }
        for c in q.order_by(PrescriptionComment.id.desc()).limit(200).all()
    ]


@router.get(
    "/comment-stats", response_model=RxCommentStatsOut, dependencies=[Depends(get_current_user)]
)
def comment_stats(db: Session = Depends(get_db)):
    """点评统计：点评覆盖数、合理率（事后监管口径）。"""
    total = db.query(func.count(PrescriptionComment.id)).scalar() or 0
    unreasonable = (
        db.query(func.count(PrescriptionComment.id))
        .filter(PrescriptionComment.grade == "unreasonable")
        .scalar()
        or 0
    )
    return {
        "commented": total,
        "unreasonable": unreasonable,
        "reasonable_rate_pct": round((total - unreasonable) * 100.0 / total, 2) if total else 0.0,
    }
