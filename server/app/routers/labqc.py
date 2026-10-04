"""检验室内质控（IQC）：质控品批号维护 → 测定值录入即判 Westgard → 失控处理闭环。

- 批号（QcLot）：项目 × 批号 × 靶值/SD，机构内唯一；停用后不再接受录入；还没有测定点的可改靶值/SD（P2-1368）；
- 测定（QcMeasurement）：录入时即按 Westgard 基础四规则判定（见 `_westgard`），
  失控点必须处理（原因 + 纠正措施）；失控未处理期间继续录入，响应给警示；
- Levey-Jennings：按批号返回时间序列 + 均值±1/2/3SD 参考线，前端画图用。

**为什么不接 formula/规则引擎**：Westgard 是文献定死的数值判定（z 分数与
相邻点比较），不是用户可配的公式——接引擎只会把四条 if 变成一套 DSL 维护负担
（CLAUDE.md §5：不再造第 7 套规则求值）。直接代码实现，判定口径见 `_westgard`。

**与 `/api/mgmt/qc`（QcRecord）法域不同**：那是①-④共享中心的运行质量台账
（人工登记合格/不合格），本模块是检验科室内质控的数值体系，互不替代。
"""
import re
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field, FiniteFloat
from sqlalchemy import exists
from sqlalchemy.orm import Session

from ..concurrency import insert_or_conflict, move_row
from ..database import get_db
from ..datetypes import OptionalDateTimeStr
from ..texttypes import NON_BLANK
from ..deps import get_current_user, paginate, require_roles
from .. import clock
from ..clock import now_local
from ..models import Organization, QcLot, QcMeasurement, User, utcnow
from ..visibility import assert_obj_org_writable, assert_org_visible, assert_org_writable, scope_org_list

router = APIRouter(prefix="/api/labqc", tags=["检验室内质控"], dependencies=[Depends(get_current_user)])


# ---------- Westgard 基础四规则（数值判定，非用户公式） ----------


def _z_score(value: float, target: float, sd: float) -> Decimal:
    """z 分数按**录入的十进制数**算（P2-156）。

    二进制浮点里 (4.2 − 4.0) / 0.1 = 2.0000000000000018、(1.3 − 1.0) / 0.1 = 3.0000000000000004：压在 ±2SD / ±3SD
    线上的测定值被判成超线——1-2s 警告，连着两次成了 2-2s 失控，4.2 之后 3.8 成了 R-4s，1.3 成了 1-3s，与下面写明的
    「z 恰为 ±2.0/±3.0 不触发」相反；靶值或 SD 带小数的批号都中招。`str(float)` 给的是能原样读回的最短写法，就是
    录入的那串数字，按它做十进制运算，压线的值恰好压线。
    """
    return (Decimal(str(value)) - Decimal(str(target))) / Decimal(str(sd))


def _westgard(z: Decimal, prev_z: Decimal | None) -> tuple[bool, bool, list[str]]:
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


def _verdict(warning: bool, out_of_control: bool, violated_rules: str) -> str:
    """判定结果的人话，与页面上的标签同一套说法。"""
    return f"失控 {violated_rules}" if out_of_control else "1-2s 警告" if warning else "在控"


#: 测定时刻的宽松读法（P2-889）：规范写法之外，兜住 P1-100（09-24）之前自由文本框里手填的不补零、斜杠写法
_LOOSE_MOMENT = re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[ T](\d{1,2}):(\d{1,2})(?::(\d{1,2}))?)?")


def _moment(value: str) -> str | None:
    """测定时刻读成可比的 `YYYY-MM-DD HH:MM:SS`，读不成日期的返回 None。"""
    matched = _LOOSE_MOMENT.fullmatch((value or "").strip())
    if matched is None:
        return None
    year, month, day, hour, minute, second = (int(part) if part else 0 for part in matched.groups())
    try:
        return datetime(year, month, day, hour, minute, second).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _time_order(m: QcMeasurement) -> tuple[str, int]:
    """测定点在时间上的先后（P2-687）：按测定时刻，同一时刻的按录入先后。

    测定时刻原先按字符串比（只把 `T` 换成空格）：P1-100 之前测定时间是自由文本框，存量里「2026-9-20 8:30」这类不补零
    的写法在第 6 位是 '9'，同一年里比任何补零的写法都「晚」（P2-889）——09-25 录的新点取不到它当上一点，真的 2-2s
    只判成警告、检验报告照发；它自己反倒被当成「时间上的下一点」改判失控，L-J 图把它画在最新一端。现在按日历读：
    不补零、斜杠写法照读；读不成日期的按录入时刻（本地）算，与留空按录入时刻同一个取法（P2-171）。一个批号的点数有限，
    在 Python 里排。
    """
    return (_moment(m.measured_at) or clock.to_local(m.created_at).strftime("%Y-%m-%d %H:%M:%S"), m.id)


# ---------- 批号维护 ----------


class LotCreate(BaseModel):
    org_id: int
    item_code: str = Field(min_length=1, max_length=64, pattern=NON_BLANK)
    item_name: str = Field(min_length=1, max_length=128, pattern=NON_BLANK)
    lot_no: str = Field(min_length=1, max_length=64, pattern=NON_BLANK)
    target_value: FiniteFloat
    sd: FiniteFloat = Field(gt=0)  # SD=0 时 z 分数除零，且质控品不可能无离散度


class LotOut(LotCreate):
    id: int
    active: bool
    # 出参不要求有限值（P1-92）：PG 的浮点/金额列存得下 NaN，存量坏值要读成 null，而不是让整个响应 500
    target_value: float
    sd: float
    # 出参不带「不能只填空格」（P1-109）：修之前存进去的纯空白行要原样读出来，而不是让整个清单 500
    item_code: str = Field(min_length=1, max_length=64)
    item_name: str = Field(min_length=1, max_length=128)
    lot_no: str = Field(min_length=1, max_length=64)

    model_config = {"from_attributes": True}


class LotPatch(BaseModel):
    """改批号：启停，或改靶值 / SD（P2-1368）。各项都可不送，一项都没送的 422。

    靶值 / SD 原先改不了（只收 `active`，送来的 `sd` 被静默忽略、照样 200）：模型注释写的是取「定值质控品说明书或前 20 次
    测定累积均值/SD」，测完却无处回填；SD 多敲一位（0.1 录成 1.0），z 分数缩成十分之一，之后永远判不出失控；停用后同一批号
    又重建不了（唯一约束）。取值约束照建批号（`LotCreate`）。认不得的键照本文件其余请求模型的缺省口径忽略。
    """

    active: bool | None = None
    target_value: FiniteFloat | None = None
    sd: FiniteFloat | None = Field(default=None, gt=0)  # 同建批号：SD=0 时 z 分数除零


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
    response: Response,
    org_id: int | None = None,
    item_code: str | None = None,
    active: bool | None = None,
    offset: int = 0,
    limit: int = 200,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = db.query(QcLot)
    q = scope_org_list(db, user, q, QcLot, org_id)
    if item_code:
        q = q.filter(QcLot.item_code == item_code)
    if active is not None:
        q = q.filter(QcLot.active.is_(active))
    return paginate(q.order_by(QcLot.id.desc()), response, offset, limit)


@router.patch(
    "/lots/{lot_id}",
    response_model=LotOut,
    dependencies=[Depends(require_roles("doctor", "operator"))],
)
def set_lot_active(lot_id: int, body: LotPatch, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """启停批号（换批后旧批停用；误停可重新启用，历史测定值保留）；还没有测定点的批号可改靶值 / SD（P2-1368）。

    靶值 / SD 是 Westgard 判定的基线：已有测定点的批号改了它，既往的判定（含登记过的失控处理）就与新基线对不上——未处理的点
    要不要按新靶值重判、已处理的怎么留痕，是业务口径，定下来之前一律 409，整条不落（一起送来的启停也不改）。「没有测定点」与
    改写压进同一条 UPDATE（`move_row`），判与改之间不留空当（正在录入、还没提交的那一点仍看不见：录入不锁批号行）。
    与现值相同的不算改。
    """
    lot = db.get(QcLot, lot_id)
    if lot is None:
        raise HTTPException(status_code=404, detail="质控批号不存在")
    assert_obj_org_writable(db, user, lot)
    if body.active is None and body.target_value is None and body.sd is None:
        raise HTTPException(status_code=422, detail="请至少改一项：启停、靶值或 SD")
    baseline = {key: value for key, value in body.model_dump(include={"target_value", "sd"}, exclude_none=True).items()
                if value != getattr(lot, key)}
    if baseline:
        values = baseline if body.active is None else {**baseline, "active": body.active}
        if not move_row(db, QcLot, lot.id, ~exists().where(QcMeasurement.lot_id == lot.id), **values):
            db.rollback()
            raise HTTPException(status_code=409, detail="该批号已有测定点，改靶值要先定既往判定怎么处理；靶值 / SD 暂不能改")
    elif body.active is not None:
        lot.active = body.active
    db.commit()
    db.refresh(lot)
    return lot


# ---------- 测定值录入（录入即判 Westgard） ----------


class MeasurementCreate(BaseModel):
    value: FiniteFloat
    # 测定时刻（补录时与录入时刻不同）；空串=以录入时刻为准
    measured_at: OptionalDateTimeStr = ""  # 时间戳真源（P1-100）：形状不对 422，合法值原样落库
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
    # 留空按录入时刻，取本地时刻（P2-171）：页面上手填的是本地时间（datetime-local），这里原先取 UTC，
    # 东八区早上 7 点半留空录的点记成前一天 23:30，同一张清单里手填的与留空的差着 8 小时。
    # 与门急诊文书的记录时间缺省同一个取法（clock.now_local：给人看的时间字符串默认值）
    measured_at = body.measured_at or now_local().strftime("%Y-%m-%d %H:%M")
    key = _moment(measured_at) or measured_at   # 入参已按 check_datetime 校验过，读得成
    # 2-2s / R-4s 比的是**时间上**相邻的两点（P2-687）。原先「上一点」按录入编号取：漏录的一次事后补录，补录点
    # 跟时间上更晚的那点比，之后录的点又跟补录点比——真的 2-2s 判成警告（不出失控处理的按钮、检验报告照发），
    # 或者凭空判出 R-4s。上一点取测定时刻不晚于本点的最后一个（同一时刻的，先录的在前）；补录插进了中间，
    # 时间上的下一点改跟本点比、重判——它还没处理时才改（处理过的失控点留着原判定与处理记录）
    in_lot = sorted(((_time_order(m), m) for m in db.query(QcMeasurement).filter(QcMeasurement.lot_id == lot.id)),
                    key=lambda pair: pair[0])
    prev = next((m for (moment, _), m in reversed(in_lot) if moment <= key), None)
    nxt = next((m for (moment, _), m in in_lot if moment > key), None)
    z = _z_score(body.value, lot.target_value, lot.sd)
    prev_z = _z_score(prev.value, lot.target_value, lot.sd) if prev is not None else None
    warning, out_of_control, violated = _westgard(z, prev_z)
    measurement = QcMeasurement(
        lot_id=lot.id,
        value=body.value,
        measured_at=measured_at,
        operator=body.operator or (user.full_name or user.username),
        warning=warning,
        out_of_control=out_of_control,
        violated_rules=";".join(violated),
    )
    db.add(measurement)
    rejudged = ""
    if nxt is not None and not nxt.handled:
        n_warning, n_out, n_violated = _westgard(_z_score(nxt.value, lot.target_value, lot.sd), z)
        before = _verdict(nxt.warning, nxt.out_of_control, nxt.violated_rules)
        after = _verdict(n_warning, n_out, ";".join(n_violated))
        # 条件写（还没处理才改）：与失控处理登记同一行，别把刚登记的处理压成「在控」
        if after != before and move_row(db, QcMeasurement, nxt.id, QcMeasurement.handled.is_(False),
                                        warning=n_warning, out_of_control=n_out, violated_rules=";".join(n_violated)):
            rejudged = f"这是补录点：插在 {nxt.measured_at} 那一点之前，该点改跟本点比，重判为「{after}」（原为「{before}」）"
    db.commit()
    db.refresh(measurement)
    out = MeasurementOut.model_validate(measurement).model_dump()
    out["unhandled_before"] = unhandled_before
    notes = [f"该批号尚有 {unhandled_before} 个失控点未处理，请先登记原因与纠正措施"] if unhandled_before else []
    out["alert"] = "；".join(notes + ([rejudged] if rejudged else []))
    return out


@router.get("/lots/{lot_id}/measurements", response_model=list[MeasurementOut])
def list_measurements(lot_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    lot = _get_lot(db, lot_id)
    assert_org_visible(db, user, lot.org_id)
    return _latest_measurements(db, lot.id)


def _latest_measurements(db: Session, lot_id: int, limit: int = 500) -> list[QcMeasurement]:
    """最近 `limit` 个测定点，按测定时刻先后排（同一时刻的按录入先后，P2-687）。

    原先按编号升序取前 500 个——截掉的恰好是最新那一端：一个批号用满 500 个点（一天两次约八个月，质控品一个批号常用
    半年到两年），第 501 个点起新录的、包括刚判出的失控点都不上页面，失控处理的按钮（按这份数据画）也就没有了，
    而每次录入都在提示「尚有 N 个失控点未处理」。与体温单（P1-81）同一个「截断截错了端」（P2-157）。
    排序按测定时刻而不是录入编号：补录的点原先排在最后，L-J 图上的连线与 Westgard 判定用的相邻关系对不上。
    """
    rows = sorted(db.query(QcMeasurement).filter(QcMeasurement.lot_id == lot_id).all(), key=_time_order)
    return rows[-limit:]   # 先后按 `_time_order`（存量不补零的测定时刻按日历读，P2-889）


# ---------- 失控处理 ----------


class HandleIn(BaseModel):
    reason: str = Field(min_length=1, max_length=512, pattern=NON_BLANK)
    corrective_action: str = Field(min_length=1, max_length=512, pattern=NON_BLANK)


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
    # 判定与写入压进同一条 UPDATE（P2-450）：原先判「已处理」、赋值、commit，UPDATE 只有 `WHERE id = ?`——两位技师
    # 对同一个失控点各点一次，两路都 200，先登记的原因、纠正措施与处理人被后到的整段盖掉（按顺序点第二下是 409）
    if not move_row(db, QcMeasurement, measurement.id, QcMeasurement.handled.is_(False),
                    handled=True, handle_reason=body.reason, corrective_action=body.corrective_action,
                    handled_by=user.full_name or user.username, handled_at=utcnow()):
        db.rollback()
        raise HTTPException(status_code=409, detail="该失控点已处理，勿重复登记")
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
    """L-J 图数据：按测定时刻排的时间序列 + 均值±1/2/3SD 参考线（前端画图用）。

    参考线以批号**靶值**为均值——L-J 图画的是"相对既定基线的漂移"，
    不是本批实测均值（那样图会跟着漂移走，失控反而看不出来）。
    """
    lot = _get_lot(db, lot_id)
    assert_org_visible(db, user, lot.org_id)
    rows = _latest_measurements(db, lot.id)
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
