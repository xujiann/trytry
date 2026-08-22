"""检验室内质控（IQC）：质控品批号维护 → 测定值录入即判 Westgard → 失控处理闭环。

- 批号（QcLot）：项目 × 批号 × 靶值/SD，机构内唯一；停用后不再接受录入；
- 测定（QcMeasurement）：录入时即按 Westgard 基础四规则判定（见 `_westgard`），
  失控点必须处理（原因 + 纠正措施）；失控未处理期间继续录入，响应给警示；
- Levey-Jennings：按批号返回时间序列 + 均值±1/2/3SD 参考线，前端画图用。

**为什么不接 formula/规则引擎**：Westgard 是文献定死的数值判定（z 分数与
相邻点比较），不是用户可配的公式——接引擎只会把四条 if 变成一套 DSL 维护负担
（CLAUDE.md §5：不再造第 7 套规则求值）。直接代码实现，判定口径见 `_westgard`。

**与 `/api/mgmt/qc`（QcRecord）法域不同**：那是①-④共享中心的运行质量台账
（人工登记合格/不合格），本模块是检验科室内质控的数值体系，互不替代。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..concurrency import insert_or_conflict
from ..database import get_db
from ..deps import get_current_user, require_roles
from ..models import Organization, QcLot, QcMeasurement, User, utcnow
from ..visibility import assert_obj_org_writable, assert_org_visible, assert_org_writable, scope_org_list

router = APIRouter(prefix="/api/labqc", tags=["检验室内质控"], dependencies=[Depends(get_current_user)])


# ---------- Westgard 基础四规则（数值判定，非用户公式） ----------


def _westgard(z: float, prev_z: float | None) -> tuple[bool, bool, list[str]]:
    """按 z 分数判定当前点：返回 (warning, out_of_control, 命中规则列表)。

    - 1-2s：|z| > 2 —— 警告（不算失控，是"启动其他规则检查"的信号）；
    - 1-3s：|z| > 3 —— 失控；
    - 2-2s：连续两点同侧超 2SD（当前与上一点 |z| 均 > 2 且同号）—— 失控；
    - R-4s：相邻两点极差超 4SD（|z - prev_z| > 4）—— 失控。

    比较一律用严格大于：z 恰为 ±2.0/±3.0 不触发（超出才算，边界测试钉住此口径）。
    """
    violated: list[str] = []
    if abs(z) > 3:
        violated.append("1-3s")
    if prev_z is not None:
        if abs(z) > 2 and abs(prev_z) > 2 and z * prev_z > 0:
            violated.append("2-2s")
        if abs(z - prev_z) > 4:
            violated.append("R-4s")
    warning = abs(z) > 2 and not violated
    return warning, bool(violated), violated


# ---------- 批号维护 ----------


class LotCreate(BaseModel):
    org_id: int
    item_code: str = Field(min_length=1, max_length=64)
    item_name: str = Field(min_length=1, max_length=128)
    lot_no: str = Field(min_length=1, max_length=64)
    target_value: float
    sd: float = Field(gt=0)  # SD=0 时 z 分数除零，且质控品不可能无离散度


class LotOut(LotCreate):
    id: int
    active: bool

    model_config = {"from_attributes": True}


class LotPatch(BaseModel):
    active: bool


@router.post(
    "/lots",
    response_model=LotOut,
    status_code=201,
    dependencies=[Depends(require_roles("doctor", "operator"))],  # 检验技师账号属医疗岗
)
def create_lot(body: LotCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    assert_org_writable(db, user, body.org_id)
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    return insert_or_conflict(
        db, QcLot(**body.model_dump()), "该机构同项目下批号已存在（换批请新建批号，旧批停用）"
    )


@router.get("/lots", response_model=list[LotOut])
def list_lots(
    org_id: int | None = None,
    item_code: str | None = None,
    active: bool | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = db.query(QcLot)
    q = scope_org_list(db, user, q, QcLot, org_id)
    if item_code:
        q = q.filter(QcLot.item_code == item_code)
    if active is not None:
        q = q.filter(QcLot.active.is_(active))
    return q.order_by(QcLot.id.desc()).limit(200).all()


@router.patch(
    "/lots/{lot_id}",
    response_model=LotOut,
    dependencies=[Depends(require_roles("doctor", "operator"))],
)
def set_lot_active(lot_id: int, body: LotPatch, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """启停批号（换批后旧批停用；误停可重新启用，历史测定值保留）。"""
    lot = db.get(QcLot, lot_id)
    if lot is None:
        raise HTTPException(status_code=404, detail="质控批号不存在")
    assert_obj_org_writable(db, user, lot)
    lot.active = body.active
    db.commit()
    db.refresh(lot)
    return lot


# ---------- 测定值录入（录入即判 Westgard） ----------


class MeasurementCreate(BaseModel):
    value: float
    # 测定时刻（补录时与录入时刻不同）；空串=以录入时刻为准
    measured_at: str = Field(default="", max_length=16)
    operator: str = Field(default="", max_length=64)


class MeasurementOut(BaseModel):
    id: int
    lot_id: int
    value: float
    measured_at: str
    operator: str
    warning: bool
    out_of_control: bool
    violated_rules: str
    handled: bool
    handle_reason: str
    corrective_action: str
    handled_by: str

    model_config = {"from_attributes": True}


class MeasurementCreateOut(MeasurementOut):
    # 该批号此前仍未处理的失控点数；>0 时 alert 给一句人话警示（失控未闭环还在测）
    unhandled_before: int
    alert: str


def _get_lot(db: Session, lot_id: int) -> QcLot:
    lot = db.get(QcLot, lot_id)
    if lot is None:
        raise HTTPException(status_code=404, detail="质控批号不存在")
    return lot


@router.post(
    "/lots/{lot_id}/measurements",
    response_model=MeasurementCreateOut,
    status_code=201,
    dependencies=[Depends(require_roles("doctor", "operator"))],
)
def create_measurement(
    lot_id: int,
    body: MeasurementCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    lot = _get_lot(db, lot_id)
    assert_obj_org_writable(db, user, lot)
    if not lot.active:
        raise HTTPException(status_code=409, detail="批号已停用，不可继续录入测定值")
    # 失控未处理警示：不拦录入（质控测定本身就是纠偏动作的一部分），但要说出来
    unhandled_before = (
        db.query(QcMeasurement)
        .filter(
            QcMeasurement.lot_id == lot.id,
            QcMeasurement.out_of_control.is_(True),
            QcMeasurement.handled.is_(False),
        )
        .count()
    )
    prev = (
        db.query(QcMeasurement)
        .filter(QcMeasurement.lot_id == lot.id)
        .order_by(QcMeasurement.id.desc())
        .first()
    )
    z = (body.value - lot.target_value) / lot.sd
    prev_z = (prev.value - lot.target_value) / lot.sd if prev is not None else None
    warning, out_of_control, violated = _westgard(z, prev_z)
    measurement = QcMeasurement(
        lot_id=lot.id,
        value=body.value,
        measured_at=body.measured_at or utcnow().strftime("%Y-%m-%d %H:%M"),
        operator=body.operator or (user.full_name or user.username),
        warning=warning,
        out_of_control=out_of_control,
        violated_rules=";".join(violated),
    )
    db.add(measurement)
    db.commit()
    db.refresh(measurement)
    out = MeasurementOut.model_validate(measurement).model_dump()
    out["unhandled_before"] = unhandled_before
    out["alert"] = (
        f"该批号尚有 {unhandled_before} 个失控点未处理，请先登记原因与纠正措施"
        if unhandled_before
        else ""
    )
    return out


@router.get("/lots/{lot_id}/measurements", response_model=list[MeasurementOut])
def list_measurements(lot_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    lot = _get_lot(db, lot_id)
    assert_org_visible(db, user, lot.org_id)
    return (
        db.query(QcMeasurement)
        .filter(QcMeasurement.lot_id == lot.id)
        .order_by(QcMeasurement.id)
        .limit(500)
        .all()
    )


# ---------- 失控处理 ----------


class HandleIn(BaseModel):
    reason: str = Field(min_length=1, max_length=512)
    corrective_action: str = Field(min_length=1, max_length=512)


@router.post(
    "/measurements/{measurement_id}/handle",
    response_model=MeasurementOut,
    dependencies=[Depends(require_roles("doctor", "operator"))],
)
def handle_measurement(
    measurement_id: int,
    body: HandleIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """失控处理登记：原因 + 纠正措施，处理人与时刻留痕。"""
    measurement = db.get(QcMeasurement, measurement_id)
    if measurement is None:
        raise HTTPException(status_code=404, detail="测定记录不存在")
    lot = _get_lot(db, measurement.lot_id)
    assert_obj_org_writable(db, user, lot)
    if not measurement.out_of_control:
        raise HTTPException(status_code=422, detail="该测定点未失控，无需处理登记")
    if measurement.handled:
        raise HTTPException(status_code=409, detail="该失控点已处理，勿重复登记")
    measurement.handled = True
    measurement.handle_reason = body.reason
    measurement.corrective_action = body.corrective_action
    measurement.handled_by = user.full_name or user.username
    measurement.handled_at = utcnow()
    db.commit()
    db.refresh(measurement)
    return measurement


# ---------- Levey-Jennings 数据 ----------


class LjPoint(BaseModel):
    id: int
    value: float
    z: float
    measured_at: str
    warning: bool
    out_of_control: bool
    violated_rules: str
    handled: bool


class LjLines(BaseModel):
    mean: float
    sd1_upper: float
    sd1_lower: float
    sd2_upper: float
    sd2_lower: float
    sd3_upper: float
    sd3_lower: float


class LjOut(BaseModel):
    lot_id: int
    item_code: str
    item_name: str
    lot_no: str
    target_value: float
    sd: float
    lines: LjLines
    points: list[LjPoint]


@router.get("/lots/{lot_id}/levey-jennings", response_model=LjOut)
def levey_jennings(lot_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """L-J 图数据：按录入顺序的时间序列 + 均值±1/2/3SD 参考线（前端画图用）。

    参考线以批号**靶值**为均值——L-J 图画的是"相对既定基线的漂移"，
    不是本批实测均值（那样图会跟着漂移走，失控反而看不出来）。
    """
    lot = _get_lot(db, lot_id)
    assert_org_visible(db, user, lot.org_id)
    rows = (
        db.query(QcMeasurement)
        .filter(QcMeasurement.lot_id == lot.id)
        .order_by(QcMeasurement.id)
        .limit(500)
        .all()
    )
    return {
        "lot_id": lot.id,
        "item_code": lot.item_code,
        "item_name": lot.item_name,
        "lot_no": lot.lot_no,
        "target_value": lot.target_value,
        "sd": lot.sd,
        "lines": {
            "mean": lot.target_value,
            "sd1_upper": round(lot.target_value + lot.sd, 6),
            "sd1_lower": round(lot.target_value - lot.sd, 6),
            "sd2_upper": round(lot.target_value + 2 * lot.sd, 6),
            "sd2_lower": round(lot.target_value - 2 * lot.sd, 6),
            "sd3_upper": round(lot.target_value + 3 * lot.sd, 6),
            "sd3_lower": round(lot.target_value - 3 * lot.sd, 6),
        },
        "points": [
            {
                "id": m.id,
                "value": m.value,
                "z": round((m.value - lot.target_value) / lot.sd, 4),
                "measured_at": m.measured_at,
                "warning": m.warning,
                "out_of_control": m.out_of_control,
                "violated_rules": m.violated_rules,
                "handled": m.handled,
            }
            for m in rows
        ],
    }
