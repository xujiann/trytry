"""E6 数据智能层：预测、慢病风险分层、DRG 费用离群侦测（纯标准库统计）。

**只用标准库**（math/statistics/collections/datetime）：本层是决策辅助，
不是精算引擎，最小二乘 / 移动平均 / 均值-标准差用纯 Python 足以说明趋势，
引入 numpy/pandas 只会给信创部署添一层依赖。

口径与既有模块对齐，不另起炉灶：
- 月度时间序列的建法与 metrics.py `/trends` 一致（同样的模型、同样的日期列、
  同样按 `YYYY-MM` 归月），只是加上了横向可见范围过滤；
- 风险评分与 chronic.py `risk_score` 一样**现算不落库**，权重取自
  RiskModelWeight（admin 配置）；
- DRG 离群与 drgs.py 一样按 drg_code 分组、同组样本不足 5 例不下结论。

所有读接口走统计可见范围 `stats_org_ids`（本机构 + 同医共体），
患者维度接口走 `assert_patient_visible`（校验即留痕）。
"""
import statistics
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from pydantic import BaseModel, Field

from ..database import get_db
from ..deps import get_current_user, require_admin, resolve_org_scope
from ..models import (
    Admission,
    CaseSummary,
    ChronicPatient,
    Encounter,
    FollowUp,
    Patient,
    Prescription,
    Referral,
    User,
    RiskModelWeight,
)
from ..visibility import stats_org_ids, assert_patient_visible, visible_org_ids

router = APIRouter(
    prefix="/api/intel", tags=["数据智能"], dependencies=[Depends(get_current_user)]
)

# 预测指标 → (模型, 日期列, 机构列)。日期列与 metrics.py /trends 同源；
# 转诊没有 org_id，机构维度取转出机构 from_org_id（与 scope_patient_list 一致）。
FORECAST_METRICS = {
    "encounters": (Encounter, Encounter.created_at, Encounter.org_id),
    "prescriptions": (Prescription, Prescription.created_at, Prescription.org_id),
    "referrals": (Referral, Referral.created_at, Referral.from_org_id),
}

# DRG 离群：同组少于该例数不下结论——参照 drgs.py MIN_BASELINE_CASES 的理由，
# 少数几例算出来的均值/标准差，报的是噪声不是异常。
MIN_DRG_CASES = 5

# 慢病风险分层阈值（占可配置权重总额的比例）。用比例而非绝对分：
# 权重由 admin 任意配置，绝对阈值会随配置漂移；比例对"触发了多重的因子"
# 才是稳定的度量。high ≥ 60%、medium ≥ 30%，其余 low。
RISK_HIGH_RATIO = 0.6
RISK_MEDIUM_RATIO = 0.3

# 因子判定阈值（近似值，可随目录细化）：
BP_SBP_LIMIT = 140.0  # 收缩压达标线
BP_DBP_LIMIT = 90.0   # 舒张压达标线
GLUCOSE_LIMIT = 7.0   # 空腹血糖达标线（mmol/L）
AGE_SENIOR = 65       # 老年阈值
ADHERENCE_POOR = 6.0  # 用药依从性评分（假定 0~10，越低越差）低于此判为依从性差


# ---------------------------------------------------------------------------
# 月度序列与可见范围
# ---------------------------------------------------------------------------


def _month_key(dt) -> str:
    return f"{dt.year:04d}-{dt.month:02d}"


def _last_months(n: int) -> list[str]:
    """近 n 个月的 `YYYY-MM` 键（含本月），与 metrics.py /trends 完全一致。"""
    today = date.today()
    keys, year, month = [], today.year, today.month
    for _ in range(n):
        keys.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return list(reversed(keys))


def _next_months(last_key: str, horizon: int) -> list[str]:
    """从 last_key 之后再推 horizon 个月的键。"""
    year, month = int(last_key[:4]), int(last_key[5:7])
    out = []
    for _ in range(horizon):
        month += 1
        if month == 13:
            year, month = year + 1, 1
        out.append(f"{year:04d}-{month:02d}")
    return out


def _effective_scope(
    db: Session, user: User, group_id: int | None, org_id: int | None
) -> list[int] | None:
    """统计可见范围 ∩ 请求范围；None 表示不加机构过滤（全域/未限定）。

    可见范围用 `stats_org_ids`（医共体片区），请求范围用 `resolve_org_scope`
    解析 group_id/org_id。请求越权的机构直接从结果里剔除（取交集），
    不报 403——预测是聚合视图，越权部分静默排除即可，与明细接口不同。
    """
    allowed = stats_org_ids(db, user)
    requested = resolve_org_scope(db, group_id, org_id)
    if requested is None:
        return allowed
    if allowed is None:
        return requested
    allowed_set = set(allowed)
    return [o for o in requested if o in allowed_set]


# ---------------------------------------------------------------------------
# 1. 业务量预测
# ---------------------------------------------------------------------------


class ForecastPoint(BaseModel):
    month: str
    value: float


@router.get("/forecast")
def forecast(
    metric: str = "encounters",
    months: int = 6,
    horizon: int = 3,
    group_id: int | None = None,
    org_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """业务量预测：移动平均 + 最小二乘趋势外推（可叠加朴素季节因子）。

    - 历史序列建法与 metrics.py /trends 一致（同模型、同日期列、按 YYYY-MM 归月），
      加机构可见范围过滤；
    - 趋势用最小二乘算 slope/intercept，外推 next `horizon` 月 = intercept+slope*idx，
      下限截到 0；
    - 历史 ≥13 个月时叠加季节因子 = 去年同月值 / 该点3期移动平均（分母为 0 时取 1.0）；
    - 有效数据点（非零月）不足 3 个时不外推，返回 history + 空 forecast + "样本不足"。
    """
    if metric not in FORECAST_METRICS:
        raise HTTPException(
            status_code=422,
            detail=f"未知指标：{metric}（可选：{'、'.join(FORECAST_METRICS)}）",
        )
    months = max(1, min(months, 24))
    horizon = max(1, min(horizon, 12))
    model, date_col, org_col = FORECAST_METRICS[metric]

    keys = _last_months(months)
    scope = _effective_scope(db, user, group_id, org_id)
    query = db.query(date_col)
    if scope is not None:
        query = query.filter(org_col.in_(scope))

    from collections import Counter

    counter = Counter(_month_key(row[0]) for row in query.all() if row[0])
    vals = [float(counter.get(k, 0)) for k in keys]
    history = [{"month": k, "value": v} for k, v in zip(keys, vals)]

    # 3 期移动平均（供输出与季节因子共用）
    def moving_avg(series: list[float], i: int, window: int = 3) -> float:
        lo = max(0, i - window + 1)
        chunk = series[lo : i + 1]
        return sum(chunk) / len(chunk) if chunk else 0.0

    ma = [round(moving_avg(vals, i), 4) for i in range(len(vals))]

    nonzero = sum(1 for v in vals if v > 0)
    if nonzero < 3:
        return {
            "metric": metric,
            "history": history,
            "moving_average": ma,
            "forecast": [],
            "slope": 0.0,
            "intercept": 0.0,
            "seasonal_applied": False,
            "note": "样本不足",
        }

    # 最小二乘：x = 月序号 0..n-1
    n = len(vals)
    xs = list(range(n))
    sx = sum(xs)
    sy = sum(vals)
    sxx = sum(x * x for x in xs)
    sxy = sum(x * y for x, y in zip(xs, vals))
    denom = n * sxx - sx * sx
    if denom == 0:  # pragma: no cover - n>=3 时不会发生
        slope = 0.0
        intercept = sy / n
    else:
        slope = (n * sxy - sx * sy) / denom
        intercept = (sy - slope * sx) / n

    seasonal_ready = n >= 13
    fc_keys = _next_months(keys[-1], horizon)
    forecast_points = []
    for h, mkey in enumerate(fc_keys, start=1):
        idx = n - 1 + h
        base = intercept + slope * idx
        factor = 1.0
        sidx = idx - 12
        if seasonal_ready and 0 <= sidx < n:
            ma_at = ma[sidx]
            if ma_at > 0:
                factor = vals[sidx] / ma_at
        value = max(0.0, base * factor)
        forecast_points.append({"month": mkey, "value": round(value, 2)})

    return {
        "metric": metric,
        "history": history,
        "moving_average": ma,
        "forecast": forecast_points,
        "slope": round(slope, 4),
        "intercept": round(intercept, 4),
        "seasonal_applied": seasonal_ready,
        "note": (
            "移动平均(窗口3)+最小二乘趋势外推"
            + ("，叠加去年同月季节因子" if seasonal_ready else "，历史不足13月未叠加季节因子")
        ),
    }


# ---------------------------------------------------------------------------
# 2. 慢病风险分层
# ---------------------------------------------------------------------------


def _patient_age(patient: Patient, today: date) -> int | None:
    """从 birth_date（YYYY-MM-DD 字符串，可能为空）粗算周岁；无法解析返回 None。"""
    raw = (patient.birth_date or "").strip()
    if len(raw) < 4:
        return None
    try:
        year = int(raw[:4])
    except ValueError:
        return None
    age = today.year - year
    return age if 0 <= age <= 150 else None


def _evaluate_factors(
    db: Session, patient: Patient, factor_names: set[str]
) -> dict[str, bool]:
    """逐因子判定是否触发。只在需要时取数，未知因子一律判为未触发。

    - bp_uncontrolled / glucose_uncontrolled / poor_adherence：取该患者所有慢病档案里
      **最近一次随访**的指标；
    - complication：任一慢病档案为 3 级（高危）视为已有并发/高危；
    - age_65：由 birth_date 现算周岁。
    """
    result = {name: False for name in factor_names}
    today = date.today()

    latest_fu = None
    if {"bp_uncontrolled", "glucose_uncontrolled", "poor_adherence"} & factor_names:
        latest_fu = (
            db.query(FollowUp)
            .join(ChronicPatient, FollowUp.chronic_id == ChronicPatient.id)
            .filter(ChronicPatient.patient_id == patient.id)
            .order_by(FollowUp.id.desc())
            .first()
        )

    if "bp_uncontrolled" in factor_names and latest_fu is not None:
        result["bp_uncontrolled"] = bool(
            (latest_fu.sbp is not None and latest_fu.sbp >= BP_SBP_LIMIT)
            or (latest_fu.dbp is not None and latest_fu.dbp >= BP_DBP_LIMIT)
        )

    if "glucose_uncontrolled" in factor_names and latest_fu is not None:
        result["glucose_uncontrolled"] = bool(
            latest_fu.glucose is not None and latest_fu.glucose >= GLUCOSE_LIMIT
        )

    if "poor_adherence" in factor_names and latest_fu is not None:
        score = (latest_fu.metrics or {}).get("adherence_score")
        if score is not None:
            try:
                result["poor_adherence"] = float(score) < ADHERENCE_POOR
            except (TypeError, ValueError):
                result["poor_adherence"] = False

    if "age_65" in factor_names:
        age = _patient_age(patient, today)
        result["age_65"] = age is not None and age >= AGE_SENIOR

    if "complication" in factor_names:
        result["complication"] = (
            db.query(ChronicPatient.id)
            .filter(ChronicPatient.patient_id == patient.id, ChronicPatient.level == 3)
            .first()
            is not None
        )

    return result


@router.get("/chronic-risk/{patient_id}")
def chronic_risk(
    patient_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """慢病风险分层：多因子加权评分，现算不落库。

    先 `assert_patient_visible`（校验即留痕）；再取 active 权重逐因子判定，
    score = Σ(被触发因子权重)。风险档位按 score 占权重总额的比例：
    ≥60% high、≥30% medium、其余 low；high 建议上转。
    未配置任何 active 权重时返回 409，避免"人人 0 分低危"这种无意义结论。
    """
    assert_patient_visible(db, user, patient_id, resource="chronic_risk")
    patient = db.get(Patient, patient_id)
    if patient is None:  # pragma: no cover - 可见性通过意味着患者存在
        raise HTTPException(status_code=404, detail="患者不存在")

    weights = db.query(RiskModelWeight).filter(RiskModelWeight.active.is_(True)).all()
    if not weights:
        raise HTTPException(status_code=409, detail="未配置风险权重")

    triggered = _evaluate_factors(db, patient, {w.factor for w in weights})
    factors = []
    score = 0.0
    total = 0.0
    for w in weights:
        hit = bool(triggered.get(w.factor, False))
        total += w.weight
        if hit:
            score += w.weight
        factors.append({"factor": w.factor, "weight": w.weight, "triggered": hit})

    ratio = score / total if total > 0 else 0.0
    if ratio >= RISK_HIGH_RATIO:
        level = "high"
    elif ratio >= RISK_MEDIUM_RATIO:
        level = "medium"
    else:
        level = "low"

    return {
        "patient_id": patient_id,
        "score": round(score, 2),
        "total_weight": round(total, 2),
        "ratio": round(ratio, 4),
        "level": level,
        "factors": factors,
        "refer_up_suggested": level == "high",
        "thresholds": {"high": RISK_HIGH_RATIO, "medium": RISK_MEDIUM_RATIO},
    }


# ---------------------------------------------------------------------------
# 3. DRG 费用离群侦测
# ---------------------------------------------------------------------------


@router.get("/anomaly/drg")
def anomaly_drg(
    multiplier: float = 2.0,
    group_id: int | None = None,
    org_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """DRG 组内费用离群：同组 case 费用 > 均值 + multiplier×总体标准差 即标记。

    口径同 drgs.py：按 drg_code 分组，同组少于 5 例不下结论（跳过）。
    标准差用总体标准差 statistics.pstdev（把本院这批病例当作总体，不做样本推断）。
    可见范围走 `stats_org_ids` ∩ 请求范围，过滤 Admission.org_id。
    """
    multiplier = max(0.0, min(multiplier, 10.0))
    scope = _effective_scope(db, user, group_id, org_id)

    query = (
        db.query(
            CaseSummary.admission_id,
            CaseSummary.drg_code,
            CaseSummary.total_cost,
        )
        .join(Admission, CaseSummary.admission_id == Admission.id)
        .filter(CaseSummary.drg_code != "")
    )
    if scope is not None:
        query = query.filter(Admission.org_id.in_(scope))

    buckets: dict[str, list[tuple[int, float]]] = {}
    for admission_id, drg_code, total_cost in query.all():
        buckets.setdefault(drg_code, []).append((admission_id, float(total_cost or 0.0)))

    groups = []
    skipped = []
    for drg_code, cases in sorted(buckets.items()):
        n = len(cases)
        if n < MIN_DRG_CASES:
            skipped.append({"drg_code": drg_code, "n": n})
            continue
        costs = [c for _, c in cases]
        mean = statistics.fmean(costs)
        std = statistics.pstdev(costs)
        threshold = mean + multiplier * std
        outliers = [
            {"admission_id": aid, "cost": round(cost, 2)}
            for aid, cost in cases
            if cost > threshold
        ]
        groups.append(
            {
                "drg_code": drg_code,
                "n": n,
                "mean": round(mean, 2),
                "std": round(std, 2),
                "threshold": round(threshold, 2),
                "outliers": sorted(outliers, key=lambda o: -o["cost"]),
            }
        )

    return {
        "multiplier": multiplier,
        "min_cases": MIN_DRG_CASES,
        "groups": groups,
        "insufficient_groups": skipped,
    }


# ---------------------------------------------------------------------------
# 4. 风险权重配置（admin）
# ---------------------------------------------------------------------------


class RiskWeightIn(BaseModel):
    factor: str = Field(min_length=1, max_length=32)
    weight: float = Field(default=0.0, ge=0)
    active: bool = True


def _weight_out(w: RiskModelWeight) -> dict:
    return {
        "id": w.id,
        "factor": w.factor,
        "weight": w.weight,
        "active": w.active,
        "created_at": w.created_at.isoformat() if w.created_at else None,
    }


@router.post("/risk-weights", dependencies=[Depends(require_admin)])
def upsert_risk_weight(body: RiskWeightIn, db: Session = Depends(get_db)):
    """按 factor 幂等 upsert 权重：已存在则更新，不存在则新建（并发下 409-safe）。"""
    existing = (
        db.query(RiskModelWeight).filter(RiskModelWeight.factor == body.factor).first()
    )
    if existing is not None:
        existing.weight = body.weight
        existing.active = body.active
        db.commit()
        db.refresh(existing)
        return _weight_out(existing)

    row = RiskModelWeight(factor=body.factor, weight=body.weight, active=body.active)
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        # 并发下另一个请求抢先插入了同一 factor：回滚后转为更新，不抛 409。
        db.rollback()
        existing = (
            db.query(RiskModelWeight)
            .filter(RiskModelWeight.factor == body.factor)
            .first()
        )
        if existing is None:  # pragma: no cover - 冲突后必存在
            raise HTTPException(status_code=409, detail="风险权重写入冲突")
        existing.weight = body.weight
        existing.active = body.active
        db.commit()
        db.refresh(existing)
        return _weight_out(existing)
    db.refresh(row)
    return _weight_out(row)


@router.get("/risk-weights")
def list_risk_weights(db: Session = Depends(get_db)):
    """风险权重清单（含停用项），供 admin 配置界面回显。"""
    rows = db.query(RiskModelWeight).order_by(RiskModelWeight.id).all()
    return [_weight_out(w) for w in rows]
