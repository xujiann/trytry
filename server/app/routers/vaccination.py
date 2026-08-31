"""㉕疫苗接种业务协同：接种记录、禁忌管理、接种前综合评估。"""
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..clock import now_naive
from ..concurrency import claim_quota
from ..datetypes import OptionalDateStr
from ..visibility import assert_org_writable, assert_patient_visible
from ..database import get_db
from ..deps import get_current_user, require_roles, resolve_business_date
from ..models import (
    Organization,
    Patient,
    User,
    VaccinationRecord,
    VaccineBatch,
    VaccineContraindication,
)

router = APIRouter(prefix="/api/vaccination", tags=["疫苗接种"], dependencies=[Depends(get_current_user)])


def _effective_contraindications(
    db: Session, patient_id: int, vaccine_code: str, today: str
) -> list[VaccineContraindication]:
    """当前真正生效的禁忌。

    过期不改状态、按日期现算：定时任务改状态会让"某条禁忌何时失效"取决于
    任务跑没跑，而这条判定直接决定能不能给人打针，不能依赖调度。
    """
    rows = (
        db.query(VaccineContraindication)
        .filter(
            VaccineContraindication.patient_id == patient_id,
            VaccineContraindication.vaccine_code == vaccine_code,
            VaccineContraindication.status == "active",
        )
        .all()
    )
    return [c for c in rows if not c.valid_until or c.valid_until >= today]


class RecordCreate(BaseModel):
    patient_id: int
    vaccine_code: str = Field(min_length=1)
    vaccine_name: str = Field(min_length=1)
    dose_no: int = Field(default=1, ge=1)
    vaccinated_date: OptionalDateStr = ""
    org_id: int
    # 批次可空（存量记录没有批号），给了就三查：过期 / 封存 / 库存
    batch_id: int | None = None
    site: str = Field(default="", max_length=32)
    vaccinator: str = Field(default="", max_length=64)


class RecordOut(RecordCreate):
    id: int
    batch_no: str = ""

    model_config = {"from_attributes": True}


@router.post(
    "/records",
    response_model=RecordOut,
    status_code=201,
    dependencies=[Depends(require_roles("doctor", "public_health"))],  # H2/L5: 疫苗接种登记
)
def vaccinate(body: RecordCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    assert_org_writable(db, user, body.org_id)
    if db.get(Patient, body.patient_id) is None:
        raise HTTPException(status_code=404, detail="受种者不存在")
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="接种机构不存在")
    # 接种禁忌硬拦截：只拦生效中的，已解除与已过期的不再拦
    forbidden = _effective_contraindications(
        db, body.patient_id, body.vaccine_code, body.vaccinated_date or date.today().isoformat()
    )
    if forbidden:
        raise HTTPException(status_code=409, detail=f"存在接种禁忌：{forbidden[0].reason}")
    # 批次三查：过期、封存、库存不足各自给出明确原因，不合并成一句
    # "批次不可用"——接种台前的人要知道是该换批次还是该补货。
    batch_no = ""
    batch = None
    if body.batch_id is not None:
        batch = db.get(VaccineBatch, body.batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail="疫苗批次不存在")
        if batch.vaccine_code != body.vaccine_code:
            raise HTTPException(status_code=422, detail="批次与所填疫苗编码不一致")
        today = body.vaccinated_date or date.today().isoformat()
        if batch.expire_date < today:
            raise HTTPException(status_code=409, detail=f"该批次已于 {batch.expire_date} 过期")
        if batch.status == "frozen":
            raise HTTPException(status_code=409, detail=f"该批次已封存：{batch.frozen_reason}")
        # 库存判定与扣减必须在同一条 SQL 里。原先是"先读再判再 += 1"，
        # 并发下每个请求都读到同一个 used_quantity、都判定还有货，
        # 最后只有一次加法生效——**实测库存 1 支打出 4 针，台账与实际对不上**，
        # 而疫苗是按批号强监管的品类。做法与预约原子占号一致。
        if not claim_quota(db, VaccineBatch, batch.id, "used_quantity", "quantity"):
            db.rollback()
            raise HTTPException(status_code=409, detail="该批次库存已用完")
        batch_no = batch.batch_no

    record = VaccinationRecord(batch_no=batch_no, **body.model_dump())
    db.add(record)
    # 扣库存与写记录同一个事务提交（D-1 的教训）
    db.commit()
    db.refresh(record)
    return record


@router.get("/records", response_model=list[RecordOut])
def vaccination_history(
    patient_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """接种史查询：临床诊疗与接种场景共享调阅。"""
    assert_patient_visible(db, user, patient_id, resource="vaccination")
    return (
        db.query(VaccinationRecord)
        .filter(VaccinationRecord.patient_id == patient_id)
        .order_by(VaccinationRecord.id.desc())
        .all()
    )


class ContraCreate(BaseModel):
    patient_id: int
    vaccine_code: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    contra_type: str = Field(default="permanent", pattern="^(permanent|temporary)$")
    valid_until: date | None = None


class ContraLift(BaseModel):
    lift_reason: str = Field(min_length=1, max_length=256)


def _contra_out(c: VaccineContraindication, today: str) -> dict:
    expired = bool(c.valid_until) and c.valid_until < today
    return {
        "id": c.id,
        "patient_id": c.patient_id,
        "vaccine_code": c.vaccine_code,
        "reason": c.reason,
        "contra_type": c.contra_type,
        "status": c.status,
        "valid_until": c.valid_until,
        "expired": expired,
        # 是否真正在拦人——状态与有效期都要看，前端不必自己再算一遍
        "blocking": c.status == "active" and not expired,
        "lift_reason": c.lift_reason,
        "lifted_at": c.lifted_at,
    }


class ContraindicationOut(BaseModel):
    """禁忌出参——`_contra_out` 的如实镜像（登记回执/清单行/解除回执/
    pre-check 的 inactive 行同一产地，共用一个模型）。

    - `valid_until` 是 `String(10)` 非空列：空串=无期限须人工解除，恒 str 非 null；
    - `lifted_at` 是 `DateTime` 可空列且**不经手工 isoformat 直接透出**：未解除
      为 null，解除后序列化为 ISO 串（与 jsonable_encoder 字节一致，医废
      `stored_at` 同款先例）；
    - `expired`/`blocking` 由日期现算不落库（见 `_effective_contraindications`）。
    """

    id: int
    patient_id: int
    vaccine_code: str
    reason: str
    contra_type: str
    status: str
    valid_until: str
    expired: bool
    blocking: bool
    lift_reason: str
    lifted_at: datetime | None


class PreVaccinationCheckOut(BaseModel):
    """接种前综合评估。`contraindications` 是**生效禁忌的 reason 字符串列表**，
    与 `inactive_contraindications` 的整行不同形——两者别合并成一个形状。"""

    allowed: bool
    contraindications: list[str]
    inactive_contraindications: list[ContraindicationOut]
    previous_doses: int
    next_dose_no: int


@router.post(
    "/contraindications",
    response_model=ContraindicationOut,
    status_code=201,
    dependencies=[Depends(require_roles("doctor", "public_health"))],  # H2/L5: 禁忌登记
)
def add_contraindication(body: ContraCreate, db: Session = Depends(get_db)):
    if db.get(Patient, body.patient_id) is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    if body.contra_type == "temporary" and body.valid_until is None:
        raise HTTPException(
            status_code=422,
            detail="暂时禁忌须给出有效期末日，否则与长期禁忌无异",
        )
    data = body.model_dump()
    data["valid_until"] = body.valid_until.isoformat() if body.valid_until else ""
    contra = VaccineContraindication(**data)
    db.add(contra)
    db.commit()
    db.refresh(contra)
    return _contra_out(contra, date.today().isoformat())


@router.get("/contraindications", response_model=list[ContraindicationOut])
def list_contraindications(
    patient_id: int,
    vaccine_code: str | None = None,
    include_lifted: bool = True,
    today: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """禁忌清单。

    默认连已解除的一起返回：接种前要回答的不只是"现在能不能打"，
    还有"以前为什么没打、后来为什么又能打"。
    """
    assert_patient_visible(db, user, patient_id, resource="vaccination")
    today_str = resolve_business_date(today).isoformat()
    query = db.query(VaccineContraindication).filter(
        VaccineContraindication.patient_id == patient_id
    )
    if vaccine_code:
        query = query.filter(VaccineContraindication.vaccine_code == vaccine_code)
    if not include_lifted:
        query = query.filter(VaccineContraindication.status == "active")
    rows = query.order_by(VaccineContraindication.id.desc()).all()
    return [_contra_out(c, today_str) for c in rows]


@router.post(
    "/contraindications/{contra_id}/lift",
    response_model=ContraindicationOut,
    dependencies=[Depends(require_roles("doctor", "public_health"))],  # H2/L5: 解除禁忌
)
def lift_contraindication(
    contra_id: int,
    body: ContraLift,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """解除禁忌（留痕不删行）。

    D-4：原先这张表只有 POST，登记后永久硬拦截，退了热也再打不了这支疫苗，
    唯一出路是改数据库。同样是"拦人"，预约黑名单一直是有 DELETE 的。
    """
    contra = db.get(VaccineContraindication, contra_id)
    if contra is None:
        raise HTTPException(status_code=404, detail="禁忌记录不存在")
    if contra.status == "lifted":
        raise HTTPException(status_code=409, detail="该禁忌已解除")
    contra.status = "lifted"
    contra.lift_reason = body.lift_reason
    contra.lifted_by = user.id
    contra.lifted_at = now_naive()
    db.commit()
    db.refresh(contra)
    return _contra_out(contra, date.today().isoformat())


@router.get("/pre-check", response_model=PreVaccinationCheckOut)
def pre_vaccination_check(
    patient_id: int,
    vaccine_code: str,
    today: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """接种前综合评估：禁忌、既往剂次、近期就诊信息一屏返回。"""
    assert_patient_visible(db, user, patient_id, resource="vaccination")
    patient = db.get(Patient, patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail="受种者不存在")
    today_str = resolve_business_date(today).isoformat()
    blocking = _effective_contraindications(db, patient_id, vaccine_code, today_str)
    history = (
        db.query(VaccineContraindication)
        .filter(
            VaccineContraindication.patient_id == patient_id,
            VaccineContraindication.vaccine_code == vaccine_code,
        )
        .order_by(VaccineContraindication.id.desc())
        .all()
    )
    doses = (
        db.query(VaccinationRecord)
        .filter(VaccinationRecord.patient_id == patient_id, VaccinationRecord.vaccine_code == vaccine_code)
        .count()
    )
    return {
        "allowed": not blocking,
        "contraindications": [c.reason for c in blocking],
        # 已解除/已过期的单列出来，不并进拦截项也不丢掉
        "inactive_contraindications": [
            _contra_out(c, today_str) for c in history if c not in blocking
        ],
        "previous_doses": doses,
        "next_dose_no": doses + 1,
    }
