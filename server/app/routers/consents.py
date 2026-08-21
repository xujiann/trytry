"""知情同意与个人信息权利（工程包 E2 · 个保法落地）——业务侧接口。

三块能力（居民端对应入口在 routers/portal.py 的 /api/portal/me/consents|corrections）：

1. **知情同意采集**：窗口代录（须附佐证 evidence）、按患者查询（过可见性）、撤回。
   同意记录只存文本版本号，全文在 ConsentText 版本库——事后答得出
   "他当时同意的是哪段话"。
2. **更正权/删除权**：窗口代提更正/注销申请 → director/admin 审核 → 通过时对
   patients 白名单字段执行变更或置 deactivated_at。写操作由审计中间件
   （main.py:audit_middleware，POST/PATCH/PUT/DELETE 全量落 AuditLog）自然留痕。
3. **未成年人**：<14 岁登记同意强制监护人三要素（复用集中审方的周岁推算与
   儿童界限，见 prescriptions.py）。

设计口径：
- 窗口登记/代提不做患者可见性**阻断**——与档案调阅授权（patients.py:
  grant_authorization）同一口径：患者本人就在柜台前，而此刻本机构往往还没有
  他的任何记录，要求先有业务关系会把这项业务办不成。写操作有 AuditLog 兜底。
- 按患者**查询**同意记录走 assert_patient_visible（判定 + AccessLog 留痕）。
"""
import json

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, paginate, require_roles
from ..models import (
    ConsentRecord,
    ConsentText,
    CorrectionRequest,
    Patient,
    User,
    utcnow,
)
from ..privacy import mask_id_card
from ..visibility import assert_patient_visible
from .prescriptions import CHILD_AGE_LIMIT, _age_of

router = APIRouter(
    prefix="/api/consents",
    tags=["知情同意与个人信息权利"],
    dependencies=[Depends(get_current_user)],
)

#: 同意采集场景（ConsentRecord.scene / ConsentText.scene 的取值范围）
CONSENT_SCENES = (
    "archive",              # 建档
    "chronic_enroll",       # 慢病入组
    "followup",             # 随访
    "family_contract",      # 家医签约
    "cross_org_access",     # 跨机构调阅
    "public_health_report", # 公卫上报
    "family_delegate",      # 家庭代管授权（居民端代管无手机号档案的第二因子）
)
SCENE_PATTERN = "^(" + "|".join(CONSENT_SCENES) + ")$"

#: 更正权白名单：允许线上更正的 patients 字段。**不含 id_card**——身份证号是
#: 主索引唯一键（EMPI 去重依据）与居民端实名绑定凭据，线上改证件号等同于把
#: 档案换到另一个人名下，必须线下人工核验证件原件后由管理员走数据订正流程。
#: 也不含 ehc_no（平台生成的对外标识，非个人信息）。patients 无地址列，
#: 故"地址类"暂无可更正项；将来加列时在此登记即可。
CORRECTABLE_FIELDS = {"name", "gender", "birth_date", "phone"}


# ============================================================================
# 共享判定：未成年人 / 代管授权 / 文本版本（portal.py 复用）
# ============================================================================


def is_minor(patient: Patient) -> bool:
    """<14 岁判定：按出生日期现算（复用集中审方的周岁推算，儿童界限同一处定义）。

    出生日期缺失/无法解析时判定为**非**未成年人：历史档案大量无出生日期，
    按未成年人拦会把成年人的正常业务全部堵死；补录出生日期是数据质控的事。
    """
    age = _age_of(patient.birth_date)
    return age is not None and age < CHILD_AGE_LIMIT


def require_guardian_for_minor(
    patient: Patient, guardian_name: str, guardian_id_card: str, guardian_relation: str
) -> None:
    """未成年人（<14 岁）登记同意/办理代管时，监护人三要素缺一即 422。"""
    if not is_minor(patient):
        return
    if not (guardian_name.strip() and guardian_id_card.strip() and guardian_relation.strip()):
        raise HTTPException(
            status_code=422,
            detail="该患者未满14周岁，须提供监护人姓名、证件号与监护关系",
        )


def has_active_delegate_consent(db: Session, patient_id: int) -> bool:
    """该档案是否有未撤回的家庭代管授权（scene=family_delegate）。"""
    return (
        db.query(ConsentRecord)
        .filter(
            ConsentRecord.patient_id == patient_id,
            ConsentRecord.scene == "family_delegate",
            ConsentRecord.revoked_at.is_(None),
        )
        .first()
        is not None
    )


def active_text_version(db: Session, scene: str) -> str:
    """场景当前生效的告知文本版本号；无（理论上种子保证有）则空串。"""
    text = (
        db.query(ConsentText)
        .filter(ConsentText.scene == scene, ConsentText.active.is_(True))
        .order_by(ConsentText.id.desc())
        .first()
    )
    return text.version if text else ""


def consent_out(record: ConsentRecord) -> dict:
    """同意记录出口形状：监护人证件号一律脱敏（PII 出口脱敏，见 privacy.py）。"""
    return {
        "id": record.id,
        "patient_id": record.patient_id,
        "scene": record.scene,
        "text_version": record.text_version,
        "method": record.method,
        "operator_user_id": record.operator_user_id,
        "resident_account_id": record.resident_account_id,
        "evidence": record.evidence,
        "guardian_name": record.guardian_name,
        "guardian_id_card": mask_id_card(record.guardian_id_card),
        "guardian_relation": record.guardian_relation,
        "revoked_at": record.revoked_at.isoformat() if record.revoked_at else "",
        "created_at": record.created_at.isoformat(),
    }


def seed_consent_texts(db: Session) -> None:
    """同意文本种子：幂等只增（按 scene+version 查已有再 add），不覆盖现场修订。

    多 worker 同时启动会并发跑种子：查重是 check-then-act，撞上
    uq_consent_text_scene_version 时说明另一个 worker 已种完，回滚即可。
    """
    from ..data.consent_texts_seed import SEED_CONSENT_TEXTS

    existing = {(scene, version) for (scene, version) in db.query(ConsentText.scene, ConsentText.version).all()}
    for seed in SEED_CONSENT_TEXTS:
        if (seed["scene"], seed["version"]) not in existing:
            db.add(ConsentText(**seed))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()


# ============================================================================
# 响应契约（CLAUDE.md §11：每个端点声明 response_model）
# ============================================================================


class ConsentOut(BaseModel):
    id: int
    patient_id: int
    scene: str
    text_version: str
    method: str
    operator_user_id: int | None
    resident_account_id: int | None
    evidence: str
    guardian_name: str
    #: 出口脱敏后的监护人证件号（保留前4后4）
    guardian_id_card: str
    guardian_relation: str
    #: 空串 = 未撤回；否则为撤回时刻 ISO 时间戳
    revoked_at: str
    created_at: str


class ConsentTextOut(BaseModel):
    id: int
    scene: str
    version: str
    content: str
    active: bool


class CorrectionOut(BaseModel):
    id: int
    patient_id: int
    request_type: str
    #: {"字段名": "新值"} 的 JSON 串；deactivate 类为空串
    changes: str
    reason: str
    status: str
    source: str
    reviewer_user_id: int | None
    review_comment: str
    created_at: str


def correction_out(req: CorrectionRequest) -> dict:
    return {
        "id": req.id,
        "patient_id": req.patient_id,
        "request_type": req.request_type,
        "changes": req.changes,
        "reason": req.reason,
        "status": req.status,
        "source": req.source,
        "reviewer_user_id": req.reviewer_user_id,
        "review_comment": req.review_comment,
        "created_at": req.created_at.isoformat(),
    }


# ============================================================================
# 知情同意：窗口代录 / 按患者查询 / 撤回 / 文本版本
# ============================================================================


class ConsentRegisterIn(BaseModel):
    patient_id: int
    scene: str = Field(pattern=SCENE_PATTERN)
    # 留空时取该场景当前生效版本；窗口录入线下纸质版可显式给纸面版本号
    text_version: str = Field(default="", max_length=16)
    # 窗口代录必附佐证：签字影像附件 id / 短信确认流水号等（接口校验非空）
    evidence: str = Field(default="", max_length=256)
    guardian_name: str = Field(default="", max_length=64)
    guardian_id_card: str = Field(default="", max_length=18)
    guardian_relation: str = Field(default="", max_length=16)


@router.post(
    "",
    response_model=ConsentOut,
    status_code=201,
    dependencies=[Depends(require_roles("operator", "doctor", "public_health"))],
)
def register_consent(
    body: ConsentRegisterIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """窗口代录知情同意。

    不做可见性阻断（与 patients.py:grant_authorization 同一口径：患者本人在
    柜台前，本机构此刻往往还没有他的记录）；写操作由审计中间件落 AuditLog。
    代录**必须附佐证**——没有佐证，事后无法证明"同意"不是经办人替填的。
    """
    if not body.evidence.strip():
        raise HTTPException(status_code=422, detail="窗口代录须附佐证材料（签字影像附件id或短信确认流水）")
    patient = db.get(Patient, body.patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    require_guardian_for_minor(
        patient, body.guardian_name, body.guardian_id_card, body.guardian_relation
    )
    record = ConsentRecord(
        patient_id=patient.id,
        scene=body.scene,
        text_version=body.text_version.strip() or active_text_version(db, body.scene),
        method="proxy",
        operator_user_id=user.id,
        evidence=body.evidence.strip(),
        guardian_name=body.guardian_name.strip(),
        guardian_id_card=body.guardian_id_card.strip(),
        guardian_relation=body.guardian_relation.strip(),
    )
    db.add(record)
    db.commit()
    return consent_out(record)


@router.get("", response_model=list[ConsentOut])
def list_consents(
    response: Response,
    patient_id: int,
    scene: str | None = None,
    offset: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """按患者查询同意记录：过可见性判定并留痕（AccessLog，resource=consent）。"""
    assert_patient_visible(db, user, patient_id, resource="consent")
    query = db.query(ConsentRecord).filter(ConsentRecord.patient_id == patient_id)
    if scene:
        query = query.filter(ConsentRecord.scene == scene)
    rows = paginate(query.order_by(ConsentRecord.id.desc()), response, offset, limit)
    return [consent_out(r) for r in rows]


@router.post(
    "/{consent_id}/revoke",
    response_model=ConsentOut,
    dependencies=[Depends(require_roles("operator", "doctor", "public_health"))],
)
def revoke_consent(consent_id: int, db: Session = Depends(get_db)):
    """撤回同意：置 revoked_at，不删行——撤回本身也要可举证。"""
    record = db.get(ConsentRecord, consent_id)
    if record is None:
        raise HTTPException(status_code=404, detail="同意记录不存在")
    if record.revoked_at is not None:
        raise HTTPException(status_code=409, detail="该同意已撤回")
    record.revoked_at = utcnow()
    db.commit()
    return consent_out(record)


@router.get("/texts", response_model=list[ConsentTextOut])
def list_consent_texts(
    scene: str | None = None,
    active_only: bool = True,
    db: Session = Depends(get_db),
):
    """同意文本版本库：窗口/居民端展示告知文本用。默认只列生效版本。"""
    query = db.query(ConsentText)
    if scene:
        query = query.filter(ConsentText.scene == scene)
    if active_only:
        query = query.filter(ConsentText.active.is_(True))
    return query.order_by(ConsentText.scene, ConsentText.id).limit(200).all()


# ============================================================================
# 更正权 / 删除权（注销）：窗口代提 → director/admin 审核 → 执行
# ============================================================================


def validate_correction_changes(request_type: str, changes: dict[str, str]) -> str:
    """校验更正内容并归一化为 JSON 串（居民端与窗口共用，口径一处定义）。"""
    if request_type == "deactivate":
        if changes:
            raise HTTPException(status_code=422, detail="注销申请无需填写更正字段")
        return ""
    if not changes:
        raise HTTPException(status_code=422, detail="更正申请须至少填写一个更正字段")
    illegal = set(changes) - CORRECTABLE_FIELDS
    if illegal:
        raise HTTPException(
            status_code=422,
            detail=(
                f"以下字段不允许线上更正：{sorted(illegal)}。"
                "身份证号涉及主索引与实名绑定，须线下核验证件原件后由管理员订正；"
                f"可更正字段：{sorted(CORRECTABLE_FIELDS)}"
            ),
        )
    for field, value in changes.items():
        if not str(value).strip():
            raise HTTPException(status_code=422, detail=f"更正字段 {field} 的新值不能为空")
    return json.dumps(changes, ensure_ascii=False)


class CorrectionSubmitIn(BaseModel):
    patient_id: int
    request_type: str = Field(default="correction", pattern="^(correction|deactivate)$")
    changes: dict[str, str] = {}
    reason: str = Field(min_length=1, max_length=256)


@router.post(
    "/corrections",
    response_model=CorrectionOut,
    status_code=201,
    dependencies=[Depends(require_roles("operator", "doctor"))],
)
def submit_correction_window(
    body: CorrectionSubmitIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """窗口代提更正/注销申请（患者到柜台口头/书面提出，经办人代录）。"""
    if db.get(Patient, body.patient_id) is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    req = CorrectionRequest(
        patient_id=body.patient_id,
        request_type=body.request_type,
        changes=validate_correction_changes(body.request_type, body.changes),
        reason=body.reason.strip(),
        source="window",
        applicant_user_id=user.id,
    )
    db.add(req)
    db.commit()
    return correction_out(req)


@router.get(
    "/corrections",
    response_model=list[CorrectionOut],
    dependencies=[Depends(require_roles("director"))],
)
def list_corrections(
    response: Response,
    status: str | None = None,
    offset: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    """待审清单（director/admin）：默认全部，可按 status=pending 等收窄。"""
    query = db.query(CorrectionRequest)
    if status:
        query = query.filter(CorrectionRequest.status == status)
    rows = paginate(query.order_by(CorrectionRequest.id.desc()), response, offset, limit)
    return [correction_out(r) for r in rows]


class CorrectionReviewIn(BaseModel):
    approve: bool
    comment: str = Field(default="", max_length=256)


@router.post(
    "/corrections/{request_id}/review",
    response_model=CorrectionOut,
    dependencies=[Depends(require_roles("director"))],
)
def review_correction(
    request_id: int,
    body: CorrectionReviewIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """审核更正/注销申请（director/admin）。

    - 通过（correction）：对 patients 白名单字段执行变更；
    - 通过（deactivate）：置 patients.deactivated_at——**注销而非删除**，医疗记录
      法定保留；此后患者检索与居民端绑定入口不再出现该档案，既有业务历史照常可查；
    - 拒绝：必须写审核意见。
    本端点是写操作，经审计中间件全量落 AuditLog（谁在何时批了哪条申请）。
    """
    req = db.get(CorrectionRequest, request_id)
    if req is None:
        raise HTTPException(status_code=404, detail="申请不存在")
    if req.status != "pending":
        raise HTTPException(status_code=409, detail=f"该申请已处理（{req.status}），不能重复审核")
    if not body.approve and not body.comment.strip():
        raise HTTPException(status_code=422, detail="拒绝申请必须填写审核意见")

    if body.approve:
        patient = db.get(Patient, req.patient_id)
        if patient is None:  # pragma: no cover - 申请提交时已校验存在，患者无物理删除
            raise HTTPException(status_code=404, detail="患者不存在")
        if req.request_type == "deactivate":
            patient.deactivated_at = utcnow()
        else:
            for field, value in json.loads(req.changes).items():
                if field in CORRECTABLE_FIELDS:  # 双保险：入库前校验过，执行时再筛一遍
                    setattr(patient, field, str(value).strip())
        req.status = "approved"
    else:
        req.status = "rejected"
    req.reviewer_user_id = user.id
    req.review_comment = body.comment.strip()
    req.reviewed_at = utcnow()
    db.commit()
    return correction_out(req)
