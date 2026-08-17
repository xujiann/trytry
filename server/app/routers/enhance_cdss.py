"""E7 临床决策支持（CDSS）深化。

三块能力，均围绕"开方/接诊时把散落的规则与档案变成可执行提示"展开：

1. 结构化过敏史（`/api/cdss/patients/{id}/allergies`）：过敏史独立成结构化载体，
   不再只作为审方诊断文本的一部分，供开方前按过敏原编码做冲突核查。
2. 交互核查升级（`/api/cdss/interaction-check`）：这是本模块的核心价值。既有
   `/api/prescriptions` 的相互作用检查只看**同一张处方内**的药对，看不见患者
   正在服用/历史开具的其他药。这里在复用 `DrugRule` 规则的基础上，把核查范围
   扩到"提交药 × 患者既往/在用处方药"，并叠加过敏冲突与重复用药，输出分级冲突。
   本接口为**咨询性（advisory）**，仅供参考，不改变处方审方状态、不拦截开方。
3. 临床路径（`/api/cdss/pathways`、`/api/cdss/patient-pathways`）：路径目录（管理员维护）
   + 患者入径 + 变异 + 出径；结合过敏史/慢病随访派生"可执行诊间提醒"。

横向隔离（A 案）：全域角色 {admin,director} 看全部；患者维度读走 `assert_patient_visible`
（判定与留痕一体）；机构维度写走 `assert_org_writable` / `assert_obj_org_writable`；
患者关联行清单走 `scope_patient_list`；路径目录属平台配置，写限 admin。
"""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, require_roles, resolve_business_date
from ..models import (
    ClinicalPathway,
    DrugRule,
    Patient,
    PathwayNode,
    PathwayVariance,
    PatientAllergy,
    PatientPathway,
    PatientPathwayOrder,
    Prescription,
    PrescriptionItem,
    User,
)
from ..concurrency import insert_or_conflict
from ..visibility import (
    assert_obj_org_visible,
    assert_obj_org_writable,
    assert_org_writable,
    assert_patient_visible,
    scope_patient_list,
)

router = APIRouter(
    prefix="/api/cdss", tags=["临床决策支持"], dependencies=[Depends(get_current_user)]
)


# ============================================================ 结构化过敏史


class AllergyIn(BaseModel):
    org_id: int
    allergen: str = Field(min_length=1, max_length=64)
    allergen_type: str = Field(default="drug", max_length=16)
    severity: str = Field(default="", max_length=8)
    note: str = Field(default="", max_length=256)


def _allergy_out(a: PatientAllergy) -> dict:
    return {
        "id": a.id,
        "patient_id": a.patient_id,
        "org_id": a.org_id,
        "allergen": a.allergen,
        "allergen_type": a.allergen_type,
        "severity": a.severity,
        "note": a.note,
        "created_at": a.created_at.isoformat() if a.created_at else "",
    }


@router.post(
    "/patients/{patient_id}/allergies",
    status_code=201,
    dependencies=[Depends(require_roles("doctor"))],
)
def add_allergy(
    patient_id: int,
    body: AllergyIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """登记结构化过敏史（医师）。写入以 org_id 机构名义，须能看该患者。"""
    if db.get(Patient, patient_id) is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    assert_patient_visible(db, user, patient_id, resource="cdss")
    assert_org_writable(db, user, body.org_id)
    allergy = PatientAllergy(patient_id=patient_id, **body.model_dump())
    db.add(allergy)
    db.commit()
    db.refresh(allergy)
    return _allergy_out(allergy)


@router.get("/patients/{patient_id}/allergies")
def list_allergies(
    patient_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """患者过敏史清单（可见即可读，留痕）。"""
    assert_patient_visible(db, user, patient_id, resource="cdss")
    rows = (
        db.query(PatientAllergy)
        .filter(PatientAllergy.patient_id == patient_id)
        .order_by(PatientAllergy.id.desc())
        .all()
    )
    return [_allergy_out(a) for a in rows]


# ============================================================ 交互核查升级


class InteractionCheckIn(BaseModel):
    patient_id: int
    drug_codes: list[str] = Field(min_length=1)
    include_history: bool = True


# 严重程度分级（advisory，仅供参考，不拦截开方）：
#   contra   —— 过敏冲突：提交药命中患者结构化过敏史（allergen_type=drug）。
#               过敏是绝对禁忌方向，故置最高级。
#   caution  —— 已知药物相互作用：提交药之间、或提交药与患者既往/在用处方药之间，
#               命中 DrugRule.interactions（任一方向）。有明确相互作用记录，需评估。
#   info     —— 重复用药：同一药品编码在提交列表内重复、或提交药已见于患者既往/在用
#               处方（同编码重复给药）。提示性，供医师核对是否有意重复。
_SEV_CONTRA = "contra"
_SEV_CAUTION = "caution"
_SEV_INFO = "info"


@router.post("/interaction-check")
def interaction_check(
    body: InteractionCheckIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """交互核查升级（仅供参考，不拦截开方）。

    在既有"同方药对"检查之上，把核查面扩展到患者既往/在用处方药与结构化过敏史：

    (a) 提交药之间的相互作用（双向查 DrugRule.interactions）→ caution；
    (b) 提交药 × 患者既往/在用处方药的相互作用（include_history 为真时）→ caution；
    (c) 重复用药：提交列表内同编码重复、或与既往处方同编码 → info；
    (d) 过敏冲突：提交药命中患者结构化过敏史（药品类）→ contra。

    分级依据见模块内 `_SEV_*` 注释。返回 `checked_against_history` 标明是否纳入历史。
    """
    assert_patient_visible(db, user, body.patient_id, resource="cdss")

    # 去重但保留输入次序，便于识别"提交列表内同编码重复"
    submitted: list[str] = [c.strip() for c in body.drug_codes if c.strip()]
    submitted_set = set(submitted)

    rules = {
        r.drug_code: r
        for r in db.query(DrugRule)
        .filter(DrugRule.drug_code.in_(submitted_set), DrugRule.active.is_(True))
        .all()
    }

    def _interactions_of(code: str) -> set[str]:
        rule = rules.get(code)
        if rule is None:
            return set()
        return {c.strip() for c in rule.interactions.split(",") if c.strip()}

    conflicts: list[dict] = []

    # (a) 提交药之间的相互作用（双向）
    seen_pairs: set[frozenset[str]] = set()
    for code in submitted_set:
        for other in _interactions_of(code) & submitted_set - {code}:
            pair = frozenset((code, other))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            conflicts.append(
                {
                    "type": "interaction",
                    "severity": _SEV_CAUTION,
                    "drug": code,
                    "against": other,
                    "detail": f"提交药 {code} 与 {other} 存在已知相互作用，需评估（仅供参考）",
                }
            )

    # (b)+(c) 患者既往/在用处方药
    history_codes: set[str] = set()
    if body.include_history:
        rows = (
            db.query(PrescriptionItem.drug_code)
            .join(Prescription, Prescription.id == PrescriptionItem.prescription_id)
            .filter(Prescription.patient_id == body.patient_id)
            .all()
        )
        history_codes = {code for (code,) in rows if code}
        # 相互作用需查历史药自身的规则（提交药可能未在提交集的 rules 里含其冲突列表）
        hist_rules = {
            r.drug_code: r
            for r in db.query(DrugRule)
            .filter(DrugRule.drug_code.in_(history_codes), DrugRule.active.is_(True))
            .all()
        }
        for code in submitted_set:
            for hcode in history_codes - {code}:
                sub_hits = hcode in _interactions_of(code)
                hist_rule = hist_rules.get(hcode)
                hist_hits = hist_rule is not None and code in {
                    c.strip() for c in hist_rule.interactions.split(",") if c.strip()
                }
                if sub_hits or hist_hits:
                    conflicts.append(
                        {
                            "type": "interaction",
                            "severity": _SEV_CAUTION,
                            "drug": code,
                            "against": hcode,
                            "detail": f"提交药 {code} 与患者既往/在用药 {hcode} 存在相互作用，需评估（仅供参考）",
                        }
                    )
        # (c) 与既往处方同编码重复给药
        for code in submitted_set & history_codes:
            conflicts.append(
                {
                    "type": "duplicate",
                    "severity": _SEV_INFO,
                    "drug": code,
                    "against": code,
                    "detail": f"提交药 {code} 与患者既往/在用处方为同一药品编码，请核对是否重复给药",
                }
            )

    # (c) 提交列表内同编码重复
    for code in sorted(submitted_set):
        if submitted.count(code) > 1:
            conflicts.append(
                {
                    "type": "duplicate",
                    "severity": _SEV_INFO,
                    "drug": code,
                    "against": code,
                    "detail": f"提交列表内药品 {code} 出现多次，请核对是否重复开具",
                }
            )

    # (d) 过敏冲突：提交药命中结构化过敏史（仅药品类过敏参与）
    allergens = {
        a.allergen
        for a in db.query(PatientAllergy)
        .filter(
            PatientAllergy.patient_id == body.patient_id,
            PatientAllergy.allergen_type == "drug",
        )
        .all()
    }
    for code in sorted(submitted_set & allergens):
        conflicts.append(
            {
                "type": "allergy",
                "severity": _SEV_CONTRA,
                "drug": code,
                "against": code,
                "detail": f"提交药 {code} 命中患者过敏史，禁用（仅供参考，请复核过敏史）",
            }
        )

    return {
        "advisory": "仅供参考",
        "conflicts": conflicts,
        "checked_against_history": bool(body.include_history),
    }


# ============================================================ 临床路径目录


class PathwayIn(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=64)
    disease: str = Field(default="", max_length=64)
    active: bool = True


class PathwayNodeIn(BaseModel):
    day_no: int = Field(default=1, ge=1)
    name: str = Field(min_length=1, max_length=64)
    order_type: str = Field(pattern="^(exam|lab|drug|nursing|other)$")
    detail: str = Field(default="", max_length=256)


def _pathway_out(p: ClinicalPathway) -> dict:
    return {
        "id": p.id,
        "code": p.code,
        "name": p.name,
        "disease": p.disease,
        "active": p.active,
    }


def _node_out(n: PathwayNode) -> dict:
    return {
        "id": n.id,
        "pathway_id": n.pathway_id,
        "day_no": n.day_no,
        "name": n.name,
        "order_type": n.order_type,
        "detail": n.detail,
    }


@router.post("/pathways", status_code=201, dependencies=[Depends(require_roles("admin"))])
def create_pathway(body: PathwayIn, db: Session = Depends(get_db)):
    """新建临床路径目录（管理员）。code 重复 409。"""
    if db.query(ClinicalPathway).filter(ClinicalPathway.code == body.code).first():
        raise HTTPException(status_code=409, detail="路径编码已存在")
    pathway = insert_or_conflict(db, ClinicalPathway(**body.model_dump()), "路径编码已存在")
    return _pathway_out(pathway)


@router.get("/pathways")
def list_pathways(active: bool | None = None, db: Session = Depends(get_db)):
    query = db.query(ClinicalPathway)
    if active is not None:
        query = query.filter(ClinicalPathway.active.is_(active))
    return [_pathway_out(p) for p in query.order_by(ClinicalPathway.id).all()]


@router.post(
    "/pathways/{pathway_id}/nodes",
    status_code=201,
    dependencies=[Depends(require_roles("admin"))],
)
def add_node(pathway_id: int, body: PathwayNodeIn, db: Session = Depends(get_db)):
    """为路径追加节点/医嘱套餐（管理员）。"""
    if db.get(ClinicalPathway, pathway_id) is None:
        raise HTTPException(status_code=404, detail="临床路径不存在")
    node = PathwayNode(pathway_id=pathway_id, **body.model_dump())
    db.add(node)
    db.commit()
    db.refresh(node)
    return _node_out(node)


@router.get("/pathways/{pathway_id}/nodes")
def list_nodes(pathway_id: int, db: Session = Depends(get_db)):
    if db.get(ClinicalPathway, pathway_id) is None:
        raise HTTPException(status_code=404, detail="临床路径不存在")
    rows = (
        db.query(PathwayNode)
        .filter(PathwayNode.pathway_id == pathway_id)
        .order_by(PathwayNode.day_no, PathwayNode.id)
        .all()
    )
    return [_node_out(n) for n in rows]


# ============================================================ 患者入径


class PatientPathwayIn(BaseModel):
    patient_id: int
    org_id: int
    pathway_id: int
    enrolled_date: str = ""


class VarianceIn(BaseModel):
    node_id: int = 0
    reason: str = Field(min_length=1, max_length=256)


def _patient_pathway_out(pp: PatientPathway) -> dict:
    return {
        "id": pp.id,
        "patient_id": pp.patient_id,
        "org_id": pp.org_id,
        "pathway_id": pp.pathway_id,
        "status": pp.status,
        "enrolled_date": pp.enrolled_date,
    }


def _pathway_order_out(o: PatientPathwayOrder) -> dict:
    return {
        "id": o.id,
        "patient_pathway_id": o.patient_pathway_id,
        "node_id": o.node_id,
        "day_no": o.day_no,
        "name": o.name,
        "order_type": o.order_type,
        "detail": o.detail,
        "status": o.status,
    }


@router.post(
    "/patient-pathways",
    status_code=201,
    dependencies=[Depends(require_roles("doctor"))],
)
def enroll_pathway(
    body: PatientPathwayIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """患者入径（医师），并按路径节点自动开立医嘱。

    以 org_id 机构名义写入，须能看该患者。入径不再只写一条 `PatientPathway`：
    把该路径的每个 `PathwayNode`（医嘱套餐）逐条物化成本患者名下的
    `PatientPathwayOrder`（status="ordered"），护士台据此看得到当日该做什么、
    可逐条 execute/cancel。返回入径记录 + 自动生成的医嘱列表与条数。
    """
    if db.get(Patient, body.patient_id) is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    pathway = db.get(ClinicalPathway, body.pathway_id)
    if pathway is None:
        raise HTTPException(status_code=404, detail="临床路径不存在")
    if not pathway.active:
        raise HTTPException(status_code=422, detail="该临床路径已停用，不可入径")
    assert_patient_visible(db, user, body.patient_id, resource="cdss")
    assert_org_writable(db, user, body.org_id)
    enrolled = body.enrolled_date or resolve_business_date(None).isoformat()
    pp = PatientPathway(
        patient_id=body.patient_id,
        org_id=body.org_id,
        pathway_id=body.pathway_id,
        enrolled_date=enrolled,
    )
    db.add(pp)
    db.flush()  # 拿到 pp.id，与下面的医嘱在同一事务内落库

    nodes = (
        db.query(PathwayNode)
        .filter(PathwayNode.pathway_id == body.pathway_id)
        .order_by(PathwayNode.day_no, PathwayNode.id)
        .all()
    )
    orders = [
        PatientPathwayOrder(
            patient_pathway_id=pp.id,
            node_id=n.id,
            day_no=n.day_no,
            name=n.name,
            order_type=n.order_type,
            detail=n.detail,
            status="ordered",
        )
        for n in nodes
    ]
    db.add_all(orders)
    db.commit()
    db.refresh(pp)
    for o in orders:
        db.refresh(o)
    out = _patient_pathway_out(pp)
    out["orders"] = [_pathway_order_out(o) for o in orders]
    out["orders_generated"] = len(orders)
    return out


@router.get("/patient-pathways/{pp_id}/orders")
def list_pathway_orders(
    pp_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """某次入径自动开立的路径医嘱清单（可见即可读，留痕）。"""
    pp = db.get(PatientPathway, pp_id)
    if pp is None:
        raise HTTPException(status_code=404, detail="患者入径记录不存在")
    assert_patient_visible(db, user, pp.patient_id, resource="cdss")
    rows = (
        db.query(PatientPathwayOrder)
        .filter(PatientPathwayOrder.patient_pathway_id == pp_id)
        .order_by(PatientPathwayOrder.day_no, PatientPathwayOrder.id)
        .all()
    )
    return [_pathway_order_out(o) for o in rows]


def _transition_pathway_order(
    order_id: int, target: str, db: Session, user: User
) -> dict:
    """路径医嘱状态推进的公共实现：仅 ordered 可流转，否则 409。"""
    order = db.get(PatientPathwayOrder, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="路径医嘱不存在")
    parent = db.get(PatientPathway, order.patient_pathway_id)
    assert_obj_org_writable(db, user, parent)
    if order.status != "ordered":
        raise HTTPException(
            status_code=409, detail=f"医嘱当前状态为 {order.status}，不可再次流转"
        )
    order.status = target
    db.commit()
    db.refresh(order)
    return _pathway_order_out(order)


@router.patch(
    "/patient-pathway-orders/{order_id}/execute",
    dependencies=[Depends(require_roles("doctor"))],
)
def execute_pathway_order(
    order_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """执行路径医嘱（医师，须能以该入径机构名义写）。非 ordered 状态 409。"""
    return _transition_pathway_order(order_id, "executed", db, user)


@router.patch(
    "/patient-pathway-orders/{order_id}/cancel",
    dependencies=[Depends(require_roles("doctor"))],
)
def cancel_pathway_order(
    order_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """作废路径医嘱（医师，须能以该入径机构名义写）。非 ordered 状态 409。"""
    return _transition_pathway_order(order_id, "cancelled", db, user)


@router.get("/patient-pathways")
def list_patient_pathways(
    org_id: int | None = None,
    patient_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = db.query(PatientPathway)
    if org_id is not None:
        query = query.filter(PatientPathway.org_id == org_id)
    query = scope_patient_list(db, user, query, PatientPathway, patient_id, "cdss")
    return [_patient_pathway_out(pp) for pp in query.order_by(PatientPathway.id.desc()).all()]


@router.post(
    "/patient-pathways/{pp_id}/variance",
    status_code=201,
    dependencies=[Depends(require_roles("doctor"))],
)
def record_variance(
    pp_id: int,
    body: VarianceIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """记录路径变异并把入径状态置为 variant（医师，须能以该入径机构名义写）。"""
    pp = db.get(PatientPathway, pp_id)
    if pp is None:
        raise HTTPException(status_code=404, detail="患者入径记录不存在")
    assert_obj_org_writable(db, user, pp)
    variance = PathwayVariance(
        patient_pathway_id=pp_id, node_id=body.node_id, reason=body.reason
    )
    pp.status = "variant"
    db.add(variance)
    db.commit()
    db.refresh(variance)
    return {
        "id": variance.id,
        "patient_pathway_id": pp_id,
        "node_id": variance.node_id,
        "reason": variance.reason,
        "status": pp.status,
    }


@router.patch(
    "/patient-pathways/{pp_id}/complete",
    dependencies=[Depends(require_roles("doctor"))],
)
def complete_pathway(
    pp_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """出径：入径状态置为 completed（医师，须能以该入径机构名义写）。"""
    pp = db.get(PatientPathway, pp_id)
    if pp is None:
        raise HTTPException(status_code=404, detail="患者入径记录不存在")
    assert_obj_org_writable(db, user, pp)
    pp.status = "completed"
    db.commit()
    db.refresh(pp)
    return _patient_pathway_out(pp)


# ============================================================ 可执行诊间提醒


@router.get("/reminders/{patient_id}")
def reminders(
    patient_id: int,
    today: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """可执行诊间提醒：把散落的到期/当日任务汇成一条条可执行提示。

    汇集两类来源：
    - 慢病/统一随访中超期未随访的任务（next_due / due_date 早于业务当天）；
    - 在径患者当天（入径日 + 节点 day_no 命中今天）应执行的路径节点医嘱。

    `today` 覆盖参数仅供测试/管理排查（YYYY-MM-DD），默认取服务端当天。
    """
    assert_patient_visible(db, user, patient_id, resource="cdss")
    today_d = resolve_business_date(today)
    today_s = today_d.isoformat()
    out: list[dict] = []

    # 超期慢病随访（复用 chronic：ChronicPatient.next_due < today）
    from ..models import ChronicPatient, FollowupTask

    overdue_chronic = (
        db.query(ChronicPatient)
        .filter(
            ChronicPatient.patient_id == patient_id,
            ChronicPatient.next_due != "",
            ChronicPatient.next_due < today_s,
        )
        .all()
    )
    for c in overdue_chronic:
        out.append(
            {
                "kind": "chronic_followup_overdue",
                "message": f"慢病（{c.disease}）随访已超期，应尽快随访",
                "due": c.next_due,
            }
        )

    # 超期统一随访任务（复用 followups：FollowupTask pending 且 due_date < today）
    overdue_tasks = (
        db.query(FollowupTask)
        .filter(
            FollowupTask.patient_id == patient_id,
            FollowupTask.status == "pending",
            FollowupTask.due_date < today_s,
        )
        .all()
    )
    for t in overdue_tasks:
        out.append(
            {
                "kind": "followup_overdue",
                "message": f"{t.title or '随访任务'}已超期未完成",
                "due": t.due_date,
            }
        )

    # 在径路径当天医嘱：入径日 + (day_no - 1) == today
    in_path = (
        db.query(PatientPathway)
        .filter(
            PatientPathway.patient_id == patient_id,
            PatientPathway.status == "in_path",
        )
        .all()
    )
    for pp in in_path:
        try:
            enrolled = date.fromisoformat(pp.enrolled_date)
        except (TypeError, ValueError):
            continue
        current_day = (today_d - enrolled).days + 1
        if current_day < 1:
            continue
        nodes = (
            db.query(PathwayNode)
            .filter(
                PathwayNode.pathway_id == pp.pathway_id,
                PathwayNode.day_no == current_day,
            )
            .order_by(PathwayNode.id)
            .all()
        )
        for n in nodes:
            out.append(
                {
                    "kind": "pathway_node_due",
                    "message": f"临床路径第{n.day_no}天医嘱：{n.name}（{n.order_type}）{n.detail}".strip(),
                    "due": today_s,
                }
            )

    return {"reminders": out, "today": today_s}
