"""远程会诊中心：申请→受理→出具意见→评价，全过程管理。"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..numtypes import MONEY_MAX, MoneyFloat
from ..texttypes import NON_BLANK
from ..visibility import assert_org_writable, assert_patient_visible
from ..concurrency import insert_or_conflict, move_row
from ..database import get_db
from ..deps import get_current_user, require_admin, require_roles
from ..models import ConsultExpert, Consultation, Organization, Patient, User
from ..schemas import (
    ConsultationAccept,
    ConsultationComplete,
    ConsultationCreate,
    ConsultationOut,
    ConsultationRate,
)

router = APIRouter(prefix="/api/consultations", tags=["远程会诊"], dependencies=[Depends(get_current_user)])

# 状态文案（措辞照抄模型列注释；报错文案用它，别把英文码直接拼给窗口人员看——P2-74）
CONSULTATION_STATUS_NAMES = {"applied": "已申请", "accepted": "已受理", "completed": "已完成", "declined": "已拒绝"}


@router.post(
    "",
    response_model=ConsultationOut,
    status_code=201,
    dependencies=[Depends(require_roles("doctor", "operator"))],  # H2: 会诊申请
)
def apply(body: ConsultationCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    assert_org_writable(db, user, body.from_org_id)  # P0-35：申请方只能是本机构；受邀方按设计是别家
    if db.get(Patient, body.patient_id) is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    for org_id, label in ((body.from_org_id, "申请"), (body.to_org_id, "受邀")):
        if db.get(Organization, org_id) is None:
            raise HTTPException(status_code=404, detail=f"{label}机构不存在")
    if body.from_org_id == body.to_org_id:
        raise HTTPException(status_code=422, detail="申请与受邀机构不能相同")
    consultation = Consultation(**body.model_dump(), created_by=user.id)
    db.add(consultation)
    db.commit()
    db.refresh(consultation)
    return consultation


@router.get("", response_model=list[ConsultationOut])
def list_consultations(
    status: str | None = None,
    patient_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """会诊清单。`patient_id` 按患者筛（P2-1193）：原先不收、照回全县最新 200 条——县医院出了意见之后全县再来 200 张会诊，
    申请方就再也找不回这张单子。按患者筛与兄弟清单同一句：先判这位患者看不看得、并留痕，再过滤（与本文件 `_get` 同一资源名）。

    不带患者号时照旧是全县最新 200 条：该按什么范围给看（申请方 / 受邀方 / 全县）随 P1-49 待裁定；在那之前不切翻页——
    切了就把这份没收口的清单从「最多 200 行」放大成「整表可翻」，再附一个全县总数（P2-8 剩余的 B 类，同一待裁定）。
    """
    query = db.query(Consultation)
    if patient_id is not None:
        assert_patient_visible(db, user, patient_id, resource="consultation")
        query = query.filter(Consultation.patient_id == patient_id)
    if status:
        query = query.filter(Consultation.status == status)
    return query.order_by(Consultation.id.desc()).limit(200).all()


def _get(db: Session, consultation_id: int, user: User) -> Consultation:
    """取会诊单，并按所属患者判可见性、留痕（P0-31）。

    原先五个流转端点都只看角色：与这张单子毫无关系的第三家机构能受理、拒绝、出具会诊
    意见（写进申请方读到的那份意见里）、评价、计费（实测各 200）。单子带申请方与受邀方
    两个机构列，两方本身就有服务关系，照常能做；这里只挡第三方。**哪一步该由哪一方做**
    （模型注释写的是「基层申请、上级接受、出具意见、申请方评价」，但从没校验）另在待裁定清单。
    先判归属再判状态，403 不泄露单据当前状态。
    """
    consultation = db.get(Consultation, consultation_id)
    if consultation is None:
        raise HTTPException(status_code=404, detail="会诊申请不存在")
    assert_patient_visible(db, user, consultation.patient_id, resource="consultation")
    return consultation


def _move(db: Session, consultation: Consultation, expect: str, action: str, **values) -> Consultation:
    """会诊单走一步：「状态还是 expect」压进同一条 UPDATE，抢输的一路按库里此刻的状态 409（P2-345）。

    原先受理 / 拒绝 / 出具意见都是「内存里判状态 → 赋值 → commit」，UPDATE 只有 `WHERE id = ?`：两位专家同时出具意见都 200，
    先写的意见被后写的整段盖掉；同时受理，受理专家记成后写的那位；拒绝与受理交错，已受理的单子被改成已拒绝。
    """
    if not move_row(db, Consultation, consultation.id, Consultation.status == expect, **values):
        db.rollback()
        db.refresh(consultation)
        raise HTTPException(status_code=409, detail=f"当前状态 {CONSULTATION_STATUS_NAMES.get(consultation.status, consultation.status)} 不可{action}")
    db.commit()
    db.refresh(consultation)
    return consultation


@router.post(
    "/{consultation_id}/accept",
    response_model=ConsultationOut,
    dependencies=[Depends(require_roles("doctor"))],  # H2: 受理属诊疗行为
)
def accept(
    consultation_id: int,
    body: ConsultationAccept,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    consultation = _get(db, consultation_id, user)
    if consultation.status != "applied":
        raise HTTPException(status_code=409, detail=f"当前状态 {CONSULTATION_STATUS_NAMES.get(consultation.status, consultation.status)} 不可受理")
    _check_expert(db, body.expert_name)
    return _move(db, consultation, "applied", "受理", status="accepted", expert_name=body.expert_name)


def _check_expert(db: Session, name: str) -> None:
    """受理专家与界面同一个规矩（P2-764）：专家库里有可排班的专家，就只能从他们里选；一位可排班的都没有才收手填。

    界面（core.js 受理会诊）早就只列可排班的专家、库为空才退回手填，注释写着「后端 accept 只收字符串不校验，于是统计里
    "谁接得多"永远是一笔糊涂账」——后端原先确实照收：页面打开之后专家被设成暂停排班，旧页面照样能选他受理；直接调接口
    填任意名字也 200，「谁接得多 / 评分」按名字分组，里面混进不存在的人。"""
    on_duty = {e.name: e.available for e in db.query(ConsultExpert).all()}
    if not any(on_duty.values()):
        return
    if name in on_duty and not on_duty[name]:
        raise HTTPException(status_code=409, detail="该专家已暂停排班，不能受理")
    if name not in on_duty:
        raise HTTPException(status_code=422, detail="受理专家须从专家库里可排班的专家中选")


@router.post(
    "/{consultation_id}/decline",
    response_model=ConsultationOut,
    dependencies=[Depends(require_roles("doctor"))],  # H2
)
def decline(consultation_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    consultation = _get(db, consultation_id, user)
    if consultation.status != "applied":
        raise HTTPException(status_code=409, detail=f"当前状态 {CONSULTATION_STATUS_NAMES.get(consultation.status, consultation.status)} 不可拒绝")
    return _move(db, consultation, "applied", "拒绝", status="declined")


@router.post(
    "/{consultation_id}/complete",
    response_model=ConsultationOut,
    dependencies=[Depends(require_roles("doctor"))],  # H2: 出具会诊意见限医师
)
def complete(
    consultation_id: int,
    body: ConsultationComplete,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    consultation = _get(db, consultation_id, user)
    if consultation.status != "accepted":
        raise HTTPException(status_code=409, detail=f"当前状态 {CONSULTATION_STATUS_NAMES.get(consultation.status, consultation.status)} 不可出具意见")
    return _move(db, consultation, "accepted", "出具意见", status="completed", opinion=body.opinion)


class ConsultationFee(BaseModel):
    fee: MoneyFloat = Field(ge=0, le=MONEY_MAX)
    fee_note: str = Field(default="", max_length=256)


class ConsultationFeeOut(BaseModel):
    """计费回执。`fee` 是 **Money 列**（Numeric asdecimal=False）：整数金额
    读回是 int——声明成 float 会把「200 元」变「200.0 元」，故 int | float。"""

    id: int
    fee: int | float
    fee_settled: bool
    fee_note: str


class ConsultationRatingStatsOut(BaseModel):
    """评分小结。`avg` 只算已评价的（真除法派生）：有评必 float、无评为 null
    （键恒在值可空，非条件键）。"""

    rated_count: int
    unrated_count: int
    avg: float | None


class ConsultationFeeStatsOut(BaseModel):
    """费用小结。`total_amount` 是 Money 求和的 `round(x, 2)` 派生：全整数时
    是 int（空表 `round(sum([]), 2)` 也是 int 0），混入小数才是 float。"""

    settled_count: int
    unsettled_count: int
    total_amount: int | float


class ConsultationStatsOut(BaseModel):
    """会诊统计。`by_status` 键是状态码、随数据变 → 宽 dict；
    `completion_rate_pct` 真除法派生：有单必 float、空表为 null。"""

    total: int
    by_status: dict[str, int]
    completion_rate_pct: float | None
    rating: ConsultationRatingStatsOut
    fee: ConsultationFeeStatsOut
    caliber: str


@router.post(
    "/{consultation_id}/fee",
    response_model=ConsultationFeeOut,
    dependencies=[Depends(require_roles("operator", "director"))],  # H2: 计费=经办/管理层
)
def settle_fee(
    consultation_id: int,
    body: ConsultationFee,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """会诊计费（指引⑤"费用管理"）。

    只有已完成的会诊可计费——拒绝与未受理的会诊没有发生服务。
    `fee=0` 与"未计费"是两回事（本院内部会诊常不计费），故用 `fee_settled`
    区分而不是拿 0 当哨兵。
    """
    consultation = _get(db, consultation_id, user)
    if consultation.status != "completed":
        raise HTTPException(status_code=409, detail="仅已完成的会诊可计费")
    consultation.fee = body.fee
    consultation.fee_note = body.fee_note
    consultation.fee_settled = True
    db.commit()
    db.refresh(consultation)
    return {
        "id": consultation.id,
        "fee": consultation.fee,
        "fee_settled": consultation.fee_settled,
        "fee_note": consultation.fee_note,
    }


@router.get("/stats", response_model=ConsultationStatsOut)
def consultation_stats(db: Session = Depends(get_db)):
    """会诊统计（指引⑤"统计分析"）：量、时效、评价与费用。

    评分均值只算**已评价**的（rating>0）：把未评价当 0 分，会让评价率越低
    分数越难看，最后逼出来的是"催评分"而不是"改服务"。
    """
    rows = db.query(Consultation).all()
    by_status: dict[str, int] = {}
    for r in rows:
        by_status[r.status] = by_status.get(r.status, 0) + 1
    rated = [r.rating for r in rows if r.rating > 0]
    settled = [r for r in rows if r.fee_settled]
    completed = by_status.get("completed", 0)
    applied_total = len(rows)
    return {
        "total": applied_total,
        "by_status": by_status,
        "completion_rate_pct": (
            round(completed * 100 / applied_total, 2) if applied_total else None
        ),
        "rating": {
            "rated_count": len(rated),
            # 未评价单列，不并进均值也不当 0 分
            "unrated_count": completed - len(rated),
            "avg": round(sum(rated) / len(rated), 2) if rated else None,
        },
        "fee": {
            "settled_count": len(settled),
            # 已完成但未计费的单列——可能是内部会诊不计费，也可能是漏计
            "unsettled_count": completed - len(settled),
            "total_amount": round(sum(r.fee for r in settled), 2),
        },
        "caliber": "评分均值只算已评价的（rating>0），未评价单列；"
                   "fee=0 与未计费是两回事，后者看 fee_settled",
    }


@router.post(
    "/{consultation_id}/rate",
    response_model=ConsultationOut,
    dependencies=[Depends(require_roles("doctor", "operator"))],  # H2: 评价代录
)
def rate(
    consultation_id: int,
    body: ConsultationRate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    consultation = _get(db, consultation_id, user)
    if consultation.status != "completed":
        raise HTTPException(status_code=409, detail="仅已完成的会诊可评价")
    consultation.rating = body.rating
    db.commit()
    db.refresh(consultation)
    return consultation

# ---------------------------------------------------------------- ADR-0006 搬家
#
# 以下自 `service_extras.py`（倾倒场）搬入：会诊专家档案。
# 路径一字未改（`/api/consultations...` 原样），两边 router 的鉴权本就一致
# （都是 `dependencies=[Depends(get_current_user)]`），故可直接并入本模块的
# router——不像 ADR-0006 第一批的 `/api/performance` 那样存在鉴权分裂。


class ConsultExpertCreatedOut(BaseModel):
    """新建只回 id——与列表的五个键不同形。"""

    id: int


class ConsultExpertOut(BaseModel):
    id: int
    name: str
    org_id: int
    specialty: str
    available: bool


# ---- ⑤ 会诊专家管理 ----


class ExpertCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=NON_BLANK)
    org_id: int
    specialty: str = Field(default="", max_length=64)
    available: bool = True


@router.post("/experts", response_model=ConsultExpertCreatedOut, status_code=201,
             dependencies=[Depends(require_admin)])
def create_expert(body: ExpertCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    assert_org_writable(db, user, body.org_id)
    # 机构得在（P2-169）：写权限守卫对全域角色直接放行、不查机构在不在——填错的编号撞外键，被翻成「专家已存在」
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    if db.query(ConsultExpert).filter(ConsultExpert.name == body.name).first():
        raise HTTPException(status_code=409, detail="专家已存在")
    e = insert_or_conflict(db, ConsultExpert(**body.model_dump()), "专家已存在")
    return {"id": e.id}


@router.get("/experts", response_model=list[ConsultExpertOut])
def list_experts(available: bool | None = None, db: Session = Depends(get_db)):
    q = db.query(ConsultExpert)
    if available is not None:
        q = q.filter(ConsultExpert.available.is_(available))
    return [{"id": e.id, "name": e.name, "org_id": e.org_id, "specialty": e.specialty, "available": e.available} for e in q.all()]