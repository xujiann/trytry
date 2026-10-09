"""⑮基层缺药登记 + ⑯居民用药监测。"""
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Query, Session

from ..concurrency import move_row
from ..numtypes import INT4_MAX
from ..texttypes import NON_BLANK, code_key
from ..visibility import assert_obj_org_writable, assert_org_writable, assert_patient_visible, can_write_org
from ..database import get_db
from ..deps import get_current_user, require_roles, row_dict
from ..clock import now_naive
from .dispense import prescription_not_reversed, q_dispensable_shortage
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

# 状态文案（措辞照抄模型列注释；报错文案用它，别把英文码直接拼给窗口人员看——P2-74）
SHORTAGE_STATUS_NAMES = {"registered": "已登记", "purchasing": "采购中", "delivered": "已配送", "collected": "已取药", "no_show": "未取药", "cancelled": "已取消"}

# 同时在用药品达到该数即提示多重用药风险
POLYPHARMACY_THRESHOLD = 5

_SHORTAGE_FLOW = {"registered": "purchasing", "purchasing": "delivered"}
# 末态：collected 与 no_show 都"结束了"，但一个是药拿走了，一个是药白调了。
# 混成一个 closed，缺药登记的履约率就永远算不出来。
_SHORTAGE_CLOSED = {"collected", "no_show", "cancelled"}
# 还缺着的：药还在路上（已登记 / 采购中）。已配送的药已经到了，结案的三态更不是供应风险（P2-127）
_SHORTAGE_SHORT = tuple(_SHORTAGE_FLOW)


class ShortageCreate(BaseModel):
    org_id: int
    # 可空：按机构报缺（补库存）与按患者登记（延伸处方）共用一张表。
    # 只有按患者登记的才谈得上"登记后不来取药"，也才进得了黑名单。
    patient_id: int | None = None
    drug_code: str = Field(min_length=1, max_length=64, pattern=NON_BLANK)
    drug_name: str = Field(min_length=1, max_length=128, pattern=NON_BLANK)
    quantity: int = Field(default=1, ge=1, le=INT4_MAX)


class ShortageOut(ShortageCreate):
    id: int
    status: str
    close_reason: str = ""
    # 出参不带「不能只填空格」（P1-109）：修之前存进去的纯空白行要原样读出来，而不是让整个清单 500
    drug_code: str = Field(min_length=1, max_length=64)
    drug_name: str = Field(min_length=1, max_length=128)
    #: 当前用户能不能流转 / 结案这条登记（以登记机构的名义写，全域角色放行；P2-793）。新增字段，页面按它摆按钮
    can_handle: bool = False
    #: 登记时刻（P2-1661，只在末尾加键，原有键与次序不动）：清单原先看不出一条登记在途多久了。落库的 naive UTC（与发药
    #: 记录 `DispenseOut.created_at` 同一个写法）。**不出患者姓名**：本清单全县可见、收口待 P1-49 裁定，全县可见的清单不先
    #: 放大患者信息；页面「患者」列印已有的 `patient_id`，姓名等收口定了再加
    created_at: datetime

    model_config = {"from_attributes": True}


def _with_can_handle(shortage: DrugShortage, user: User) -> DrugShortage:
    """挂上 `can_handle` 供响应模型取用（不入库）：与流转 / 结案的 `assert_obj_org_writable` 同一判据（P2-793）。
    清单是全县的，原先页面只看状态摆「流转」「结案」，别家的登记照样有，点了必 403。按角色摆不摆随 P2-447 待裁定。"""
    setattr(shortage, "can_handle", can_write_org(user, shortage.org_id))
    return shortage


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
    return _with_can_handle(shortage, user)


@router.get("/shortages", response_model=list[ShortageOut])
def list_shortages(status: str | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    query = db.query(DrugShortage)
    if status:
        query = query.filter(DrugShortage.status == status)
    return [_with_can_handle(s, user) for s in query.order_by(DrugShortage.id.desc()).limit(200).all()]


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
        # 已配送不是终态（还要结案），原先同报「已是终态」、页面却照样给它摆着结案按钮（P2-353）
        detail = ("已配送的登记不能再推进，请结案" if shortage.status == "delivered"
                  else f"状态 {SHORTAGE_STATUS_NAMES.get(shortage.status, shortage.status)} 已是终态")
        raise HTTPException(status_code=409, detail=detail)
    # 走一步压进带状态条件的 UPDATE（P2-317）：原先锁外读改写，推进与结案交错时，后提交的推进照自己读到的旧状态写——
    # 已取消的登记被翻回「已配送」（结案时间与原因还挂在上面），又能再判一次取药与否
    if not _move_shortage(db, shortage, status=next_status):
        raise HTTPException(
            status_code=409,
            detail=f"登记状态已变为 {SHORTAGE_STATUS_NAMES.get(shortage.status, shortage.status)}，请刷新后再操作")
    db.commit()
    db.refresh(shortage)
    return _with_can_handle(shortage, user)


def _move_shortage(db: Session, shortage: DrugShortage, **values: Any) -> bool:
    """缺药登记走一步：状态还是判过的那个才写（`concurrency.move_row`）；抢输了回滚并把对象刷成库里此刻的样子，
    调用方按它报 409。"""
    if move_row(db, DrugShortage, shortage.id, DrugShortage.status == shortage.status, **values):
        return True
    db.rollback()
    db.refresh(shortage)
    return False


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
        raise HTTPException(status_code=409, detail=f"该登记已结案（{SHORTAGE_STATUS_NAMES.get(shortage.status, shortage.status)}）")
    if body.result in ("collected", "no_show") and shortage.status != "delivered":
        raise HTTPException(status_code=409, detail="药品尚未配送到位，不可判定取药与否")
    # 同上（P2-317）：两人同时结案（一个判已取药、一个判未取药），原先都 200，库里只剩后写的那个结论
    if not _move_shortage(db, shortage, status=body.result, close_reason=body.reason, closed_at=now_naive()):
        raise HTTPException(
            status_code=409,
            detail=f"登记状态已变为 {SHORTAGE_STATUS_NAMES.get(shortage.status, shortage.status)}，请刷新后再操作")
    db.commit()
    db.refresh(shortage)
    return _with_can_handle(shortage, user)


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
    ).order_by(DrugShortage.status).all()
    by_status = row_dict(rows)
    collected = by_status.get("collected", 0)
    no_show = by_status.get("no_show", 0)
    settled = collected + no_show
    return {
        "by_status": by_status,
        # 在途只算药还在路上的（已登记 / 采购中，与缺药预警同一个 `_SHORTAGE_SHORT`）：已配送的药已经到了。原先把已配送也加进来，
        # 与本函数说明「在途……药还没到」相反，页面卡片「在途」多数一截（P2-353）
        "in_transit": sum(by_status.get(status, 0) for status in _SHORTAGE_SHORT),
        "collected": collected,
        "no_show": no_show,
        "fulfillment_rate_pct": (
            round(collected * 100 / settled, 2) if settled else None
        ),
        "caliber": "履约率分母只含已判定取药与否的登记（collected + no_show），"
                   "在途与已取消不计；无可判定登记时返回 null 而非 0",
    }


class MedicationProfileDrugOut(BaseModel):
    """用药画像的药品行：`max_daily_dose` 来自 Float 列（整数剂量 5/10 读回 5.0/10.0，
    `max(0.0, …)` 也不改型），恒 float——与 Money 列相反，判据是列类型。

    `in_use`：有一张开这味药的处方，按它的用药天数算，今天还在服药期内（P2-144）。"""

    drug_code: str
    drug_name: str
    times: int
    max_daily_dose: float
    in_use: bool


class MedicationProfileOut(BaseModel):
    patient_id: int
    distinct_drugs: int
    #: 其中在用的品种数——多重用药预警按它判（P2-144）
    in_use_drugs: int
    polypharmacy_warning: bool
    drugs: list[MedicationProfileDrugOut]
    #: 患者姓名（P2-1774，只加在末尾、原有键与次序不动）：画像按手输的患者号查，原先只回编号——敲错一位，看的是别人的在用药，
    #: 页面只写「患者 2」认不出来。查看前已判可见性并留痕，回姓名不扩大可见范围（同就诊行 P2-1631）
    patient_name: str


@router.get("/profile/{patient_id}", response_model=MedicationProfileOut)
def medication_profile(
    patient_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """居民用药画像：历次通过审方的处方里的药品（标出在用的）+ 多重用药预警。

    预警按**同时在用**的品种数判（`POLYPHARMACY_THRESHOLD` 的本义）：一味药只要有一张处方按用药天数算、
    今天还在服药期内，就算在用。原先数的是历次处方里的全部品种——十年里断续吃过五种短程药的居民，
    今天一种没吃也挂着「多重用药风险」（P2-144）。
    """
    assert_patient_visible(db, user, patient_id, resource="medication")
    patient = db.get(Patient, patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    rows = (
        db.query(PrescriptionItem, Prescription.created_at)
        .join(Prescription, PrescriptionItem.prescription_id == Prescription.id)
        .filter(
            Prescription.patient_id == patient_id,
            Prescription.status.in_(["auto_passed", "approved"]),
            # 退药冲销的没有用上（P2-624）：原先照算次数、照算「在用」，只退不重开的也挂着这味药
            prescription_not_reversed(),
        )
        .all()
    )
    now = now_naive()
    drugs: dict[str, dict] = {}
    # 「次」按处方数（P2-354）：同一张处方里同一味药可以有两行（审方后补开、分次用法），原先每行记一次
    prescriptions: dict[str, set[int]] = {}
    for item, prescribed_at in rows:
        # 同一味药按编码的比对键归并（P2-1773，与审方认规则的 P1-218 同一个 `code_key`）：编码是开方时的手输框，续方写成
        # `c09aa02`、`C08CA05 `、全角的原样落库，原先按原样分组——一味药算成两种在用，在用品种数虚高、误报多重用药。显示的
        # 编码与药名取组内字典序最小的写法：药名原先取无序查询里先遇到的那一行，换个库、换个执行计划就换一个
        key = code_key(item.drug_code)
        entry = drugs.setdefault(
            key, {"drug_code": item.drug_code, "drug_name": item.drug_name, "times": 0,
                  "max_daily_dose": 0.0, "in_use": False}
        )
        entry["drug_code"] = min(entry["drug_code"], item.drug_code)
        entry["drug_name"] = min(entry["drug_name"], item.drug_name)
        seen = prescriptions.setdefault(key, set())
        if item.prescription_id not in seen:
            seen.add(item.prescription_id)
            entry["times"] += 1
        entry["max_daily_dose"] = max(entry["max_daily_dose"], item.daily_dose)
        # 用秒数比而不是 prescribed_at + timedelta(days=…)：用药天数上限是 INT4_MAX，timedelta 装不下
        if (now - prescribed_at).total_seconds() < item.days * 86400:
            entry["in_use"] = True
    drug_list = sorted(drugs.values(), key=lambda d: d["times"], reverse=True)
    in_use = sum(1 for d in drug_list if d["in_use"])
    return {
        "patient_id": patient_id,
        "distinct_drugs": len(drug_list),
        "in_use_drugs": in_use,
        "polypharmacy_warning": in_use >= POLYPHARMACY_THRESHOLD,
        "drugs": drug_list,
        "patient_name": patient.name,   # 认人键（P2-1774）
    }


class DrugUsageStatOut(BaseModel):
    """用药地图行：rx_count/patient_count 恒 int（COUNT），声明成 float 即改字节。"""

    drug_code: str
    drug_name: str
    rx_count: int
    patient_count: int


@router.get("/usage-stats", response_model=list[DrugUsageStatOut])
def usage_stats(db: Session = Depends(get_db)):
    """全县用药地图：品种使用排名，支撑药品需求预测与供应保障。

    按药品编码归并、「方」数按处方数（P2-354）：原先按（编码, 药名）分组、数处方明细行——药名是每行自由填的，同一味药
    换个写法就拆成两行排名；同一张处方里这味药有两行就记成两张方。药名取同编码里按字典序最小的那个写法（稳定、可复现）。

    编码也按比对键归并（P2-1773，与审方认规则的 P1-218 同一个 `code_key`）：编码是开方时的手输框，`c09aa02`、`C09AA02 `、
    全角的原样落库，原先 SQL 按原样分组，同一味药又拆成几行排名。比对键（全角转半角、去零宽字符）SQL 里算不出来：先按原样
    编码分组计数，再在这里按比对键并组，显示的编码与药名都取组内字典序最小的写法；排序（方数降序、同数按编码）与前 50 名的
    上限照旧，挪到并组之后做。
    """
    by_code = (
        _counted_items(db.query(
            PrescriptionItem.drug_code,
            func.min(PrescriptionItem.drug_name),
            func.count(func.distinct(PrescriptionItem.prescription_id)),
            func.count(func.distinct(Prescription.patient_id)),
        ))
        .group_by(PrescriptionItem.drug_code)
        .order_by(PrescriptionItem.drug_code)   # 分组键排成全序（test_groupby_order）：并组时各写法的先后两个库一致
        .all()
    )
    groups: dict[str, list[tuple[str, str, int, int]]] = {}
    for code, name, rx_count, patient_count in by_code:
        groups.setdefault(code_key(code), []).append((code, name, rx_count, patient_count))
    # 一组只有一种写法的（绝大多数）SQL 的去重计数就是答案；有几种写法的，方数与人数要跨写法去重——同一张方里两种写法
    # 各一行、同一位患者两种写法都开过，按写法各数再相加就多算了。只对这几组回表取（处方, 患者）
    spelled = [spelling[0] for spellings in groups.values() if len(spellings) > 1 for spelling in spellings]
    rx_ids: dict[str, set[int]] = {}
    patient_ids: dict[str, set[int]] = {}
    if spelled:
        for code, rx_id, patient_id in (
            _counted_items(
                db.query(PrescriptionItem.drug_code, PrescriptionItem.prescription_id, Prescription.patient_id))
            .filter(PrescriptionItem.drug_code.in_(spelled))
            .distinct()
        ):
            rx_ids.setdefault(code_key(code), set()).add(rx_id)
            patient_ids.setdefault(code_key(code), set()).add(patient_id)
    stats: list[dict[str, Any]] = []
    for key, spellings in groups.items():
        if len(spellings) == 1:
            code, name, rx_count, patient_count = spellings[0]
        else:
            code, name = min(s[0] for s in spellings), min(s[1] for s in spellings)
            rx_count, patient_count = len(rx_ids[key]), len(patient_ids[key])
        stats.append({"drug_code": code, "drug_name": name, "rx_count": rx_count, "patient_count": patient_count})
    stats.sort(key=lambda s: (-s["rx_count"], s["drug_code"]))
    return stats[:50]


def _counted_items(query: Query) -> Query:
    """用药地图数的处方明细：审方通过、没有退药冲销的处方（P2-624）。并组前后两次取数共用这一个口径（P2-1773）。"""
    return query.join(Prescription, PrescriptionItem.prescription_id == Prescription.id).filter(
        Prescription.status.in_(["auto_passed", "approved"]), prescription_not_reversed())


class SupplyRiskItemOut(BaseModel):
    """供应风险行：有库存告警的 `drug_name` 取库存行的名字；仅缺药登记出现的药品取这些还缺着的登记上按字典序最小的
    那个写法（口径同用药地图，P2-354）——原先写死空串，登记表上明明有药名，页面却印「—」（P2-1664）。
    `open_shortages` 数的是还缺着的登记（已登记 / 采购中，P2-127）。"""

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
    """⑯药品供应风险评估：库存低于阈值 + 还缺着的缺药登记数 → 分级风险。

    「还缺着」= 已登记 / 采购中。原先按 `status != 已配送` 数（P2-127）：流转后来加了取药 / 未取药 / 取消三个终态，
    这三态全被算成缺药——已经取走药的登记让胰岛素照旧挂「高风险」，只剩结案登记的药品也凭空列成中风险。

    「库存告警」按**可发量**低于阈值判，与缺药预警同一个构造（`dispense.q_dispensable_shortage`，P2-1250）：原先比汇总，
    批次过了效期汇总一片不少，发药 409，这里照旧不算风险。按库存行编号排：左连之后库给的先后不再是表序，同一药品在
    几家机构的药名不一时，取哪家的名字要稳定。

    同一味药按编码的比对键认（P2-1773，与审方认规则的 P1-218 同一个 `code_key`）：库存与缺药登记的编码都是手输框，小写、
    尾随空格、全角原样落库，原先按原样分组——甲院库存告警写 `A10AE04`、乙院登记写 `Ａ１０ＡＥ０４`，拆成两条「中」，漏报
    「高」。按比对键并组：库存告警数机构（同一家几种写法只算 1 家），登记条数相加；显示的编码取组内字典序最小的写法，药名
    照旧——有库存告警的取库存行的名字，否则取登记上字典序最小的写法。
    """
    low_stocks = [
        s for s, _ in q_dispensable_shortage(db).filter(DrugStock.threshold > 0).order_by(DrugStock.id).all()
    ]
    # 药名在同一条查询里顺手取（P2-1664）：同编码的登记药名是各自手填的，取字典序最小的那个写法，稳定、可复现
    open_shortage_rows = (
        db.query(DrugShortage.drug_code, func.count(DrugShortage.id), func.min(DrugShortage.drug_name))
        .filter(DrugShortage.status.in_(_SHORTAGE_SHORT))
        .group_by(DrugShortage.drug_code)
        .order_by(DrugShortage.drug_code)
        .all()
    )
    risks: dict[str, dict[str, Any]] = {}
    low_orgs: dict[str, set[int]] = {}
    for s in low_stocks:
        key = code_key(s.drug_code)
        entry = risks.setdefault(
            key, {"drug_code": s.drug_code, "drug_name": s.drug_name, "low_stock_orgs": 0, "open_shortages": 0},
        )
        entry["drug_code"] = min(entry["drug_code"], s.drug_code)
        low_orgs.setdefault(key, set()).add(s.org_id)
        entry["low_stock_orgs"] = len(low_orgs[key])
    for code, n, name in open_shortage_rows:
        entry = risks.setdefault(
            code_key(code), {"drug_code": code, "drug_name": name, "low_stock_orgs": 0, "open_shortages": 0}
        )
        entry["drug_code"] = min(entry["drug_code"], code)
        if not entry["low_stock_orgs"]:
            entry["drug_name"] = min(entry["drug_name"], name)
        entry["open_shortages"] += n
    results = []
    for entry in risks.values():
        # 既有缺药登记又库存告警=高风险；仅其一=中风险
        entry["risk_level"] = (
            "high" if entry["low_stock_orgs"] and entry["open_shortages"] else "medium"
        )
        results.append(entry)
    results.sort(key=lambda e: (e["risk_level"] != "high", e["drug_code"]))
    return {"total": len(results), "risks": results}
