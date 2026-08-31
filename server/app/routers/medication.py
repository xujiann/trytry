"""⑮基层缺药登记 + ⑯居民用药监测。"""
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..visibility import assert_obj_org_writable, assert_org_writable, assert_patient_visible
from ..database import get_db
from ..deps import get_current_user, require_roles, row_dict
from ..clock import now_naive
from ..models import (
    DrugShortage,
    DrugStock,
    Organization,
    Patient,
    Prescription,
    PrescriptionItem,
    ServiceBlacklist,
    User,
)

router = APIRouter(prefix="/api/medication", tags=["药事监测"], dependencies=[Depends(get_current_user)])

# 同时在用药品达到该数即提示多重用药风险
POLYPHARMACY_THRESHOLD = 5

_SHORTAGE_FLOW = {"registered": "purchasing", "purchasing": "delivered"}
# 末态：collected 与 no_show 都"结束了"，但一个是药拿走了，一个是药白调了。
# 混成一个 closed，缺药登记的履约率就永远算不出来。
_SHORTAGE_CLOSED = {"collected", "no_show", "cancelled"}


class ShortageCreate(BaseModel):
    org_id: int
    # 可空：按机构报缺（补库存）与按患者登记（延伸处方）共用一张表。
    # 只有按患者登记的才谈得上"登记后不来取药"，也才进得了黑名单。
    patient_id: int | None = None
    drug_code: str = Field(min_length=1)
    drug_name: str = Field(min_length=1)
    quantity: int = Field(default=1, ge=1)


class ShortageOut(ShortageCreate):
    id: int
    status: str
    close_reason: str = ""

    model_config = {"from_attributes": True}


class ShortageClose(BaseModel):
    # collected=已取药, no_show=未取药, cancelled=已取消
    result: str = Field(pattern="^(collected|no_show|cancelled)$")
    reason: str = Field(default="", max_length=256)


@router.post(
    "/shortages",
    response_model=ShortageOut,
    status_code=201,
    dependencies=[Depends(require_roles("operator", "pharmacist"))],  # H2: 短缺登记
)
def register_shortage(body: ShortageCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    assert_org_writable(db, user, body.org_id)
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="登记机构不存在")
    if body.patient_id is not None:
        if db.get(Patient, body.patient_id) is None:
            raise HTTPException(status_code=404, detail="患者不存在")
        # 黑名单硬拦截：缺药登记要走临时采购，登记了不来取药，
        # 这批药就砸在基层手里。与预约爽约同一条逻辑。
        banned = (
            db.query(ServiceBlacklist)
            .filter(
                ServiceBlacklist.domain == "shortage",
                ServiceBlacklist.patient_id == body.patient_id,
            )
            .first()
        )
        if banned:
            raise HTTPException(
                status_code=403, detail=f"该患者在缺药登记黑名单中：{banned.reason}"
            )
    shortage = DrugShortage(**body.model_dump())
    db.add(shortage)
    db.commit()
    db.refresh(shortage)
    return shortage


@router.get("/shortages", response_model=list[ShortageOut])
def list_shortages(status: str | None = None, db: Session = Depends(get_db)):
    query = db.query(DrugShortage)
    if status:
        query = query.filter(DrugShortage.status == status)
    return query.order_by(DrugShortage.id.desc()).limit(200).all()


@router.post(
    "/shortages/{shortage_id}/advance",
    response_model=ShortageOut,
    dependencies=[Depends(require_roles("operator", "pharmacist"))],  # H2: 短缺流转
)
def advance_shortage(shortage_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    shortage = db.get(DrugShortage, shortage_id)
    if shortage is None:
        raise HTTPException(status_code=404, detail="缺药登记不存在")
    assert_obj_org_writable(db, user, shortage)
    next_status = _SHORTAGE_FLOW.get(shortage.status)
    if next_status is None:
        raise HTTPException(status_code=409, detail=f"状态 {shortage.status} 已是终态")
    shortage.status = next_status
    db.commit()
    db.refresh(shortage)
    return shortage


@router.post(
    "/shortages/{shortage_id}/close",
    response_model=ShortageOut,
    dependencies=[Depends(require_roles("operator", "pharmacist"))],
)
def close_shortage(shortage_id: int, body: ShortageClose, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """使用管理（指引⑮"使用管理"）：药到了，取没取。

    取消可以在任何阶段发生（患者转院、药源已解决）；取药与未取药只能在
    配送到位之后判定——药还没到就说"未取药"是冤枉人。
    """
    shortage = db.get(DrugShortage, shortage_id)
    if shortage is None:
        raise HTTPException(status_code=404, detail="缺药登记不存在")
    assert_obj_org_writable(db, user, shortage)
    if shortage.status in _SHORTAGE_CLOSED:
        raise HTTPException(status_code=409, detail=f"该登记已结案（{shortage.status}）")
    if body.result in ("collected", "no_show") and shortage.status != "delivered":
        raise HTTPException(status_code=409, detail="药品尚未配送到位，不可判定取药与否")
    shortage.status = body.result
    shortage.close_reason = body.reason
    shortage.closed_at = now_naive()
    db.commit()
    db.refresh(shortage)
    return shortage


class ShortageStatsOut(BaseModel):
    """缺药登记统计（test_medication_contract.py 逐字节取证）。

    `by_status` 宽键窄值：键面由数据里出现过的状态决定（metrics.by_level 先例），
    值恒为 COUNT。`fulfillment_rate_pct` 是「键恒在值可空」→ `float | None`：
    无可判定登记时就是 null（caliber 里写明的口径），有分母时真除法恒 float——
    不是条件键，无需 exclude_unset。
    """

    by_status: dict[str, int]
    in_transit: int
    collected: int
    no_show: int
    fulfillment_rate_pct: float | None
    caliber: str


@router.get("/shortages/stats", response_model=ShortageStatsOut)
def shortage_stats(db: Session = Depends(get_db)):
    """缺药登记统计：履约率与在途量。

    履约率的分母只算**已配送**的（取药 + 未取药），不含在途与已取消——
    药还没到就算进分母，等于拿采购周期长短去背履约率的锅。
    """
    rows = db.query(DrugShortage.status, func.count(DrugShortage.id)).group_by(
        DrugShortage.status
    ).all()
    by_status = row_dict(rows)
    collected = by_status.get("collected", 0)
    no_show = by_status.get("no_show", 0)
    settled = collected + no_show
    return {
        "by_status": by_status,
        "in_transit": by_status.get("registered", 0) + by_status.get("purchasing", 0)
        + by_status.get("delivered", 0),
        "collected": collected,
        "no_show": no_show,
        "fulfillment_rate_pct": (
            round(collected * 100 / settled, 2) if settled else None
        ),
        "caliber": "履约率分母只含已判定取药与否的登记（collected + no_show），"
                   "在途与已取消不计；无可判定登记时返回 null 而非 0",
    }


class MedicationProfileDrugOut(BaseModel):
    """在用药品行：`max_daily_dose` 来自 Float 列（整数剂量 5/10 读回 5.0/10.0，
    `max(0.0, …)` 也不改型），恒 float——与 Money 列相反，判据是列类型。"""

    drug_code: str
    drug_name: str
    times: int
    max_daily_dose: float


class MedicationProfileOut(BaseModel):
    patient_id: int
    distinct_drugs: int
    polypharmacy_warning: bool
    drugs: list[MedicationProfileDrugOut]


@router.get("/profile/{patient_id}", response_model=MedicationProfileOut)
def medication_profile(
    patient_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """居民用药画像：在用药品清单（通过审方的处方）+ 多重用药预警。"""
    assert_patient_visible(db, user, patient_id, resource="medication")
    if db.get(Patient, patient_id) is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    rows = (
        db.query(PrescriptionItem, Prescription.status)
        .join(Prescription, PrescriptionItem.prescription_id == Prescription.id)
        .filter(
            Prescription.patient_id == patient_id,
            Prescription.status.in_(["auto_passed", "approved"]),
        )
        .all()
    )
    drugs: dict[str, dict] = {}
    for item, _status in rows:
        entry = drugs.setdefault(
            item.drug_code, {"drug_code": item.drug_code, "drug_name": item.drug_name, "times": 0, "max_daily_dose": 0.0}
        )
        entry["times"] += 1
        entry["max_daily_dose"] = max(entry["max_daily_dose"], item.daily_dose)
    drug_list = sorted(drugs.values(), key=lambda d: d["times"], reverse=True)
    return {
        "patient_id": patient_id,
        "distinct_drugs": len(drug_list),
        "polypharmacy_warning": len(drug_list) >= POLYPHARMACY_THRESHOLD,
        "drugs": drug_list,
    }


class DrugUsageStatOut(BaseModel):
    """用药地图行：rx_count/patient_count 恒 int（COUNT），声明成 float 即改字节。"""

    drug_code: str
    drug_name: str
    rx_count: int
    patient_count: int


@router.get("/usage-stats", response_model=list[DrugUsageStatOut])
def usage_stats(db: Session = Depends(get_db)):
    """全县用药地图：品种使用排名，支撑药品需求预测与供应保障。"""
    rows = (
        db.query(
            PrescriptionItem.drug_code,
            PrescriptionItem.drug_name,
            func.count(PrescriptionItem.id).label("rx_count"),
            func.count(func.distinct(Prescription.patient_id)).label("patient_count"),
        )
        .join(Prescription, PrescriptionItem.prescription_id == Prescription.id)
        .filter(Prescription.status.in_(["auto_passed", "approved"]))
        .group_by(PrescriptionItem.drug_code, PrescriptionItem.drug_name)
        .order_by(func.count(PrescriptionItem.id).desc())
        .limit(50)
        .all()
    )
    return [
        {"drug_code": r.drug_code, "drug_name": r.drug_name, "rx_count": r.rx_count, "patient_count": r.patient_count}
        for r in rows
    ]


class SupplyRiskItemOut(BaseModel):
    """供应风险行：仅缺药登记出现的药品 `drug_name` 是空串（不回填库存名，照抄
    现状）；`open_shortages` 的口径是 `status != "delivered"`（终态登记也计入）。"""

    drug_code: str
    drug_name: str
    low_stock_orgs: int
    open_shortages: int
    risk_level: str


class SupplyRiskOut(BaseModel):
    total: int
    risks: list[SupplyRiskItemOut]


@router.get("/supply-risk", response_model=SupplyRiskOut)
def supply_risk(db: Session = Depends(get_db)):
    """⑯药品供应风险评估：库存低于阈值 + 未结案缺药登记数 → 分级风险。"""
    low_stocks = (
        db.query(DrugStock)
        .filter(DrugStock.threshold > 0, DrugStock.quantity < DrugStock.threshold)
        .all()
    )
    open_shortage_rows = (
        db.query(DrugShortage.drug_code, func.count(DrugShortage.id))
        .filter(DrugShortage.status != "delivered")
        .group_by(DrugShortage.drug_code)
        .all()
    )
    shortage_by_code = {code: n for code, n in open_shortage_rows}
    risks: dict[str, dict[str, Any]] = {}
    for s in low_stocks:
        entry = risks.setdefault(
            s.drug_code,
            {"drug_code": s.drug_code, "drug_name": s.drug_name, "low_stock_orgs": 0, "open_shortages": 0},
        )
        entry["low_stock_orgs"] += 1
    for code, n in shortage_by_code.items():
        entry = risks.setdefault(
            code, {"drug_code": code, "drug_name": "", "low_stock_orgs": 0, "open_shortages": 0}
        )
        entry["open_shortages"] = n
    results = []
    for entry in risks.values():
        # 既有缺药登记又库存告警=高风险；仅其一=中风险
        entry["risk_level"] = (
            "high" if entry["low_stock_orgs"] and entry["open_shortages"] else "medium"
        )
        results.append(entry)
    results.sort(key=lambda e: (e["risk_level"] != "high", e["drug_code"]))
    return {"total": len(results), "risks": results}
