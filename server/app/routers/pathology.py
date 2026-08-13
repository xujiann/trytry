"""④病理标本核收与标本管理。

第七轮子功能级重审发现：`exams.advance_sample` 明确 `center_type != "lab"`
即 422，病理标本整段没有状态机——而指引第 4 条头两项就是"病理标本核收"
"标本管理"。

**不与检验样本共用一套流转**。检验是采样→冷链转运→中心核收，病理是
核收→取材→制片→阅片，节点数与含义都不同。硬套一套只会得到一个两头不像的
状态机（与专病不复用慢病同一条理由）。

**拒收是核心业务而非异常分支**。标本量不足、未加固定液、标识不清都要当场
拒收并说明理由——不拒收，后面做出来的片子是废的，而报告已经发出去了。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..concurrency import insert_with_retry
from ..database import get_db
from ..deps import get_current_user, require_roles
from ..models import ExamRequest, PathologySpecimen

router = APIRouter(
    prefix="/api/pathology", tags=["病理标本"], dependencies=[Depends(get_current_user)]
)

# 病理标本流转：核收后依次取材制片阅片。拒收是岔出去的终态，不在这条链上。
SPECIMEN_FLOW = {"received": "embedded", "embedded": "slided", "slided": "read"}
SPECIMEN_STATUS = {
    "pending": "待核收",
    "received": "已核收",
    "embedded": "已取材",
    "slided": "已制片",
    "read": "已阅片",
    "rejected": "已拒收",
}
REJECT_REASONS = ["标本量不足", "未加固定液", "标识不清", "标本破损", "申请单信息不符"]


class SpecimenIn(BaseModel):
    request_id: int
    site: str = Field(default="", max_length=128)
    excised_at: str = Field(default="", max_length=19)
    fixed_at: str = Field(default="", max_length=19)
    fixative: str = Field(default="", max_length=64)
    note: str = Field(default="", max_length=512)


class SpecimenReceive(BaseModel):
    received_by: str = Field(min_length=1, max_length=64)


class SpecimenReject(BaseModel):
    reject_reason: str = Field(min_length=1, max_length=256)


class SpecimenAdvance(BaseModel):
    block_count: int = Field(default=0, ge=0)
    slide_count: int = Field(default=0, ge=0)


def _cold_ischemia_minutes(specimen: PathologySpecimen) -> int | None:
    """冷缺血时间（离体到固定），病理质控的核心指标。

    任一时间未填即返回 None——**不拿当前时间凑数**。填不全就是没采集到，
    单列报出比编一个数字诚实（与 DDD 未维护、职称等级未填同一条原则）。
    """
    if not specimen.excised_at or not specimen.fixed_at:
        return None
    from datetime import datetime

    try:
        excised = datetime.fromisoformat(specimen.excised_at)
        fixed = datetime.fromisoformat(specimen.fixed_at)
    except ValueError:
        return None
    delta = int((fixed - excised).total_seconds() // 60)
    return delta if delta >= 0 else None


def _out(s: PathologySpecimen) -> dict:
    return {
        "id": s.id,
        "request_id": s.request_id,
        "specimen_no": s.specimen_no,
        "site": s.site,
        "excised_at": s.excised_at,
        "fixed_at": s.fixed_at,
        "fixative": s.fixative,
        "cold_ischemia_minutes": _cold_ischemia_minutes(s),
        "status": s.status,
        "status_name": SPECIMEN_STATUS.get(s.status, s.status),
        "reject_reason": s.reject_reason,
        "received_by": s.received_by,
        "block_count": s.block_count,
        "slide_count": s.slide_count,
        "note": s.note,
    }


def _next_specimen_no(db: Session) -> str:
    """标本号 P-序号。由平台生成，理由同医废追溯码：唯一性是全部价值。"""
    used = db.query(func.count(PathologySpecimen.id)).scalar() or 0
    return f"P{used + 1:06d}"


def _specimen(db: Session, specimen_id: int) -> PathologySpecimen:
    specimen = db.get(PathologySpecimen, specimen_id)
    if specimen is None:
        raise HTTPException(status_code=404, detail="标本不存在")
    return specimen


@router.post(
    "/specimens", status_code=201, dependencies=[Depends(require_roles("doctor", "operator"))]
)
def submit_specimen(body: SpecimenIn, db: Session = Depends(get_db)):
    """送检登记。一张病理申请单可对应多个标本（多部位取材），故不做一对一约束。"""
    request = db.get(ExamRequest, body.request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="检查申请不存在")
    if request.center_type != "pathology":
        raise HTTPException(status_code=422, detail="仅病理申请有标本流转环节")
    # 与医废追溯码同理：标本号由服务端顺序生成，冲突该由服务端重试。
    # 原先返回 409"标本号冲突，请重试"——把服务端的分配问题推给了送检的人。
    specimen = insert_with_retry(
        db,
        lambda: PathologySpecimen(specimen_no=_next_specimen_no(db), **body.model_dump()),
    )
    return _out(specimen)


@router.get("/specimens")
def list_specimens(
    request_id: int | None = None, status: str | None = None, db: Session = Depends(get_db)
):
    query = db.query(PathologySpecimen)
    if request_id is not None:
        query = query.filter(PathologySpecimen.request_id == request_id)
    if status:
        query = query.filter(PathologySpecimen.status == status)
    return [_out(s) for s in query.order_by(PathologySpecimen.id.desc()).limit(500).all()]


@router.post(
    "/specimens/{specimen_id}/receive", dependencies=[Depends(require_roles("doctor", "operator"))]
)
def receive_specimen(specimen_id: int, body: SpecimenReceive, db: Session = Depends(get_db)):
    """核收。核收人必填——标本出了问题，要找得到当时是谁签的收。"""
    specimen = _specimen(db, specimen_id)
    if specimen.status != "pending":
        raise HTTPException(status_code=409, detail=f"当前状态 {specimen.status} 不可核收")
    specimen.status = "received"
    specimen.received_by = body.received_by
    db.commit()
    db.refresh(specimen)
    return _out(specimen)


@router.post(
    "/specimens/{specimen_id}/reject", dependencies=[Depends(require_roles("doctor", "operator"))]
)
def reject_specimen(specimen_id: int, body: SpecimenReject, db: Session = Depends(get_db)):
    """拒收。只能在核收环节拒——已经开始取材制片了才说标本不合格，晚了。"""
    specimen = _specimen(db, specimen_id)
    if specimen.status != "pending":
        raise HTTPException(
            status_code=409, detail=f"当前状态 {specimen.status} 不可拒收（拒收只能发生在核收环节）"
        )
    specimen.status = "rejected"
    specimen.reject_reason = body.reject_reason
    db.commit()
    db.refresh(specimen)
    return _out(specimen)


@router.post(
    "/specimens/{specimen_id}/advance", dependencies=[Depends(require_roles("doctor", "operator"))]
)
def advance_specimen(specimen_id: int, body: SpecimenAdvance, db: Session = Depends(get_db)):
    """推进：核收→取材→制片→阅片。蜡块数与切片数在对应环节记录。"""
    specimen = _specimen(db, specimen_id)
    if specimen.status == "rejected":
        raise HTTPException(status_code=409, detail="已拒收的标本不可推进")
    next_status = SPECIMEN_FLOW.get(specimen.status)
    if next_status is None:
        raise HTTPException(
            status_code=409,
            detail="待核收的标本请先核收" if specimen.status == "pending" else "该标本已阅片",
        )
    specimen.status = next_status
    if next_status == "embedded" and body.block_count:
        specimen.block_count = body.block_count
    if next_status == "slided" and body.slide_count:
        specimen.slide_count = body.slide_count
    db.commit()
    db.refresh(specimen)
    return _out(specimen)


@router.get("/specimen-stats")
def specimen_stats(db: Session = Depends(get_db)):
    """标本质控统计：拒收率与冷缺血时间。"""
    by_status = dict(
        db.query(PathologySpecimen.status, func.count(PathologySpecimen.id))
        .group_by(PathologySpecimen.status)
        .all()
    )
    total = sum(by_status.values())
    rejected = by_status.get("rejected", 0)
    rows = db.query(PathologySpecimen).all()
    minutes = [m for m in (_cold_ischemia_minutes(s) for s in rows) if m is not None]
    return {
        "total": total,
        "by_status": {
            k: {"count": v, "name": SPECIMEN_STATUS.get(k, k)} for k, v in by_status.items()
        },
        "rejected": rejected,
        "reject_rate_pct": round(rejected * 100 / total, 2) if total else None,
        "cold_ischemia": {
            "measured": len(minutes),
            # 时间没填全的单列，不拿当前时间凑数
            "unmeasured": total - len(minutes),
            "avg_minutes": round(sum(minutes) / len(minutes), 1) if minutes else None,
            "over_60min": len([m for m in minutes if m > 60]),
        },
        "reject_reason_options": REJECT_REASONS,
        "caliber": "冷缺血时间＝离体到固定；离体或固定时间未填的不参与均值，"
                   "单列为 unmeasured",
    }
